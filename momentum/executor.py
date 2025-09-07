# momentum/executor.py
from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any

from redis.asyncio import Redis

from common.exchange import Exchange, ExchangeError
from metrics.server import (
    observe_order_latency_ms,
    observe_slippage_bps,
    inc_rejected_orders,
)

# Tipos mínimos esperados de la estrategia
class MomentumStrategy:  # interfaz esperada
    symbol: str

    def update_candle(self, close: float, high: float, low: float) -> None: ...
    def generate_signal(
        self, funding_rate_h: float, long_max: float, short_min: float
    ) -> tuple[str, float, float]: ...
    def get_current_atr(self) -> Optional[float]: ...
    def get_last_close(self) -> Optional[float]: ...


@dataclass
class Position:
    side: str           # "LONG" | "SHORT"
    entry_price: float
    qty: float
    stop_price: float
    stop_order_id: Optional[str] = None


class MomentumExecutor:
    """
    Ejecuta la estrategia Momentum sobre perps:
      - Entrada a mercado + STOP_MARKET reduce-only
      - Trailing por ATR
      - Filtro de funding (umbrales o percentiles)
      - Ventana no-trade alrededor de funding cut (00/08/16 UTC)
      - Lock de capital en Redis (drain si se pierde)
    """

    def __init__(self, exchange: Exchange, strategy: MomentumStrategy, redis: Redis, bot_id: str):
        self.exchange = exchange
        self.strategy = strategy
        self.redis = redis
        self.bot_id = bot_id

        self.active_position: Optional[Position] = None
        self.state: str = "ACTIVE"  # "ACTIVE" | "PAUSED"
        self.consecutive_losses: int = 0
        self.last_trailing_ts_ms: int = 0
        self.last_order_ts_ms: int = 0

        # Config
        self._slippage_cap = float(os.getenv("SLIPPAGE_CAP", "0.002"))  # 0.2%
        self._loss_streak_limit = int(os.getenv("LOSS_STREAK_LIMIT", "3"))
        self._no_trade_window_s = int(os.getenv("NO_TRADE_FUNDING_WINDOW_S", "120"))
        self._order_timeout_ms = int(os.getenv("ORDER_TIMEOUT_MS", "2000"))

        self._capital_lock_key = os.getenv("CAPITAL_LOCK_KEY", "capital:lock")

    # ---------- Hooks de mercado ----------

    async def on_new_candle(self, close: float, high: float, low: float) -> None:
        """
        Llamar al cerrar la vela principal (TF_PRIMARY). Decide entradas.
        """
        if self.state == "PAUSED" or not await self._check_lock():
            return

        # Ventana no-trade alrededor del funding cut
        if self._within_no_trade_window():
            return

        # Actualizar indicadores
        self.strategy.update_candle(close, high, low)

        # Funding por hora (decimal). Fuente: Redis 'funding:{SYMBOL}' (set por datafeed/funding.py)
        f_raw = await self.redis.get(f"funding:{self.strategy.symbol}")
        f_rate_h = float(f_raw.decode() if isinstance(f_raw, (bytes, bytearray)) else (f_raw or 0.0))

        # Umbrales de funding
        long_max = float(os.getenv("FUNDING_LONG_MAX", "0.0"))
        short_min = float(os.getenv("FUNDING_SHORT_MIN", "0.0"))
        if os.getenv("USE_FUNDING_PERCENTILES", "false").lower() == "true":
            # Leer percentiles precalculados
            p_long = await self.redis.get(f"funding:pctl:long:{self.strategy.symbol}")
            p_short = await self.redis.get(f"funding:pctl:short:{self.strategy.symbol}")
            if p_long is not None:
                long_max = float(p_long.decode() if isinstance(p_long, (bytes, bytearray)) else p_long)
            if p_short is not None:
                short_min = float(p_short.decode() if isinstance(p_short, (bytes, bytearray)) else p_short)

        # Generar señal (signal: "LONG" | "SHORT" | "NONE"; qty en contratos; stop_price)
        signal, qty, stop_price = self.strategy.generate_signal(f_rate_h, long_max, short_min)

        if signal in ("LONG", "SHORT") and self.active_position is None and qty > 0:
            await self._open_position(signal, qty, stop_price)

    async def on_price_update(self, last_price: float) -> None:
        """
        Llamar en ticks frecuentes (p.ej. TF_SECONDARY o markPrice).
        Ajusta trailing-stop.
        """
        if not self.active_position:
            return

        # Anti-spam trailing: máx una actualización cada 2 s
        now_ms = int(time.time() * 1000)
        if now_ms - self.last_trailing_ts_ms < 2000:
            return

        atr = self._get_atr_fallback()
        if atr is None:
            return

        trail_mult = float(os.getenv("ATR_TRAIL_MULT", "1.2"))
        trail_dist = trail_mult * atr

        pos = self.active_position
        if pos.side == "LONG":
            new_stop = max(pos.stop_price, last_price - trail_dist)
            if new_stop > pos.stop_price:
                await self._move_stop(new_stop, is_long=True)
        else:
            # SHORT: si cae el precio, acercamos stop por arriba (entry - price > trail)
            new_stop = min(pos.stop_price, last_price + trail_dist)
            if new_stop < pos.stop_price:
                await self._move_stop(new_stop, is_long=False)

        self.last_trailing_ts_ms = now_ms

    # ---------- Core de ejecución ----------

    async def _open_position(self, signal: str, quantity: float, stop_price: float) -> None:
        side = "BUY" if signal == "LONG" else "SELL"
        client_id = f"{self.bot_id}-{self.strategy.symbol}-{int(time.time()*1000)}"

        start = time.perf_counter()
        try:
            order_resp = await self.exchange.place_order(
                symbol=self.strategy.symbol,
                side=side,
                order_type="MARKET",
                quantity=quantity,
                client_id=client_id,
            )
        except ExchangeError:
            inc_rejected_orders(1)
            return
        finally:
            observe_order_latency_ms((time.perf_counter() - start) * 1000.0)

        fill_price = self._extract_fill_price(order_resp)
        last_cached = await self._get_last_price()
        last_price = fill_price if last_cached is None else last_cached

        # Slippage cap
        if last_price > 0:
            slip = abs(fill_price - last_price) / last_price
            observe_slippage_bps(slip * 1e4)
            if slip > self._slippage_cap:
                # rollback entrada
                await self._rollback_entry(order_resp)
                inc_rejected_orders(1)
                return

        # Crear STOP_MARKET reduce-only opuesto
        stop_side = "SELL" if signal == "LONG" else "BUY"
        stop_client_id = f"{client_id}-SL"
        stop_id: Optional[str] = None
        try:
            stop_resp = await self.exchange.place_order(
                symbol=self.strategy.symbol,
                side=stop_side,
                order_type="STOP_MARKET",
                quantity=quantity,
                stop_price=stop_price,
                client_id=stop_client_id,
            )
            stop_id = str(stop_resp.get("orderId", "")) if isinstance(stop_resp, dict) else None
        except ExchangeError:
            # si no hay stop, intentamos cerrar la entrada
            await self._rollback_entry(order_resp)
            inc_rejected_orders(1)
            return

        # Registrar posición activa
        self.active_position = Position(
            side=signal, entry_price=fill_price, qty=quantity, stop_price=stop_price, stop_order_id=stop_id
        )
        self.last_order_ts_ms = int(time.time() * 1000)

        # TODO: persistir en DuckDB (orders/positions) y actualizar métricas PnL/fees si corresponde

    async def _rollback_entry(self, entry_resp: Dict[str, Any]) -> None:
        """
        Reversión de la entrada si falló el stop o slippage excesivo.
        Si hubo fill parcial, cierra la cantidad ejecutada.
        """
        order_id = entry_resp.get("orderId")
        executed = float(entry_resp.get("executedQty", 0.0))

        if executed > 0:
            # cerrar la porción ejecutada
            orig_side = (entry_resp.get("side") or "").upper()
            opp_side = "SELL" if orig_side == "BUY" else "BUY"
            try:
                await self.exchange.place_order(
                    symbol=self.strategy.symbol,
                    side=opp_side,
                    order_type="MARKET",
                    quantity=executed,
                    client_id=f"rollback-{order_id}-{int(time.time()*1000)}",
                )
            except ExchangeError:
                # Log crítico: posible intervención manual
                pass
        else:
            # nada ejecutado: cancelamos
            try:
                if order_id:
                    await self.exchange.cancel_order(self.strategy.symbol, order_id=order_id)
            except ExchangeError:
                pass

        self.active_position = None

    async def _move_stop(self, new_stop_price: float, is_long: bool) -> None:
        """
        Mueve el STOP_MARKET existente al nuevo precio (cancel + nueva orden).
        """
        if not self.active_position:
            return

        # Cancelar stop anterior si tenemos ID
        if self.active_position.stop_order_id:
            try:
                await self.exchange.cancel_order(self.strategy.symbol, order_id=self.active_position.stop_order_id)
            except ExchangeError:
                # si no existe, seguimos
                pass

        side = "SELL" if is_long else "BUY"
        qty = self.active_position.qty
        client_id = f"trail-{self.bot_id}-{int(time.time()*1000)}"

        try:
            resp = await self.exchange.place_order(
                symbol=self.strategy.symbol,
                side=side,
                order_type="STOP_MARKET",
                quantity=qty,
                stop_price=new_stop_price,
                client_id=client_id,
            )
            self.active_position.stop_order_id = str(resp.get("orderId", "")) if isinstance(resp, dict) else None
            self.active_position.stop_price = new_stop_price
        except ExchangeError:
            # si falla mover el stop, no cambiamos el estado local
            pass

    # ---------- Lock & control ----------

    async def _check_lock(self) -> bool:
        """
        Verifica el lock de capital. Si no lo tenemos, hace pause+drain.
        """
        val = await self.redis.get(self._capital_lock_key)
        owner = val.decode() if isinstance(val, (bytes, bytearray)) else val
        if not owner or owner != self.bot_id:
            await self.pause_and_drain()
            return False
        return True

    async def pause_and_drain(self) -> None:
        """
        Pone el bot en PAUSED y cierra posición activa con reduce-only.
        Publica evento 'drained'.
        """
        self.state = "PAUSED"
        if self.active_position:
            side_close = "SELL" if self.active_position.side == "LONG" else "BUY"
            try:
                await self.exchange.place_order(
                    symbol=self.strategy.symbol,
                    side=side_close,
                    order_type="MARKET",
                    quantity=self.active_position.qty,
                    client_id=f"drain-{int(time.time()*1000)}",
                )
            except ExchangeError:
                pass
            self.active_position = None

        try:
            await self.redis.xadd("events", {"event": "drained", "bot": self.bot_id})
        except Exception:
            pass

    def record_trade_result(self, pnl: float) -> None:
        """
        Actualiza racha de pérdidas/ganancias y aplica pausa por racha.
        (P&L diario agregado lo hará riskd.)
        """
        if pnl < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0

        if self.consecutive_losses >= self._loss_streak_limit:
            self.state = "PAUSED"

    # ---------- Utilidades ----------

    def _within_no_trade_window(self) -> bool:
        """
        Devuelve True si estamos dentro de ±NO_TRADE_FUNDING_WINDOW_S
        respecto de los cortes 00/08/16 UTC.
        """
        w = self._no_trade_window_s
        if w <= 0:
            return False

        now = datetime.utcnow().replace(tzinfo=timezone.utc)
        # próximos cortes del día (00,08,16)
        cuts = []
        for h in (0, 8, 16):
            cuts.append(now.replace(hour=h, minute=0, second=0, microsecond=0))
        # Añadir cortes del día siguiente y anterior por bordes
        tomorrow = now + timedelta(days=1)
        yesterday = now - timedelta(days=1)
        for h in (0, 8, 16):
            cuts.append(tomorrow.replace(hour=h, minute=0, second=0, microsecond=0))
            cuts.append(yesterday.replace(hour=h, minute=0, second=0, microsecond=0))

        # ¿a menos de w segundos de algún corte?
        for c in cuts:
            if abs((now - c).total_seconds()) <= w:
                return True
        return False

    def _extract_fill_price(self, order_resp: Dict[str, Any]) -> float:
        """
        Intenta obtener el precio promedio de fill desde la respuesta.
        """
        # Binance futures puede devolver avgPrice
        p = order_resp.get("avgPrice")
        if p:
            try:
                return float(p)
            except Exception:
                pass
        # o lista de fills
        fills = order_resp.get("fills")
        if isinstance(fills, list) and fills:
            try:
                return float(fills[0].get("price"))
            except Exception:
                pass
        # último recurso: usar close de estrategia
        last_close = self.strategy.get_last_close() or 0.0
        return float(last_close)

    async def _get_last_price(self) -> Optional[float]:
        """
        Lee 'price:last:{SYMBOL}' de Redis (escrito por market_ws).
        """
        raw = await self.redis.get(f"price:last:{self.strategy.symbol}")
        if raw is None:
            return None
        try:
            return float(raw.decode() if isinstance(raw, (bytes, bytearray)) else raw)
        except Exception:
            return None
