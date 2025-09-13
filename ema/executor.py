# ema/executor.py
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional, Dict, Any

from common.exchange import Exchange, ExchangeError
from ema.strategy import EmaCrossStrategy
from ema.config import EmaConfig

# Métricas (si no existe metrics.server, quedan en no-op; el runner expone /metrics)
try:
    from metrics.server import observe_order_latency_ms, inc_rejected_orders
except Exception:  # pragma: no cover
    def observe_order_latency_ms(_ms: float) -> None: ...
    def inc_rejected_orders(_n: int) -> None: ...


@dataclass
class Position:
    side: str              # "LONG" | "SHORT"
    qty: float
    entry_price: float
    stop_order_id: Optional[str] = None


class EmaExecutor:
    """
    Ejecutor simple:
      - En cruce LONG abre BUY MARKET por 'alloc_usdt / price'
      - En cruce opuesto cierra posición (y opcionalmente abre SHORT si allow_shorts)
      - Stop-loss fijo (STOP_PCT) vía STOP_MARKET reduceOnly (si STOP_PCT > 0)
    """
    def __init__(self, exchange: Exchange, strategy: EmaCrossStrategy, cfg: EmaConfig) -> None:
        self.exchange = exchange
        self.strategy = strategy
        self.cfg = cfg
        self.position: Optional[Position] = None

    # ---- helpers ----
    async def _qty_from_alloc(self, price: float) -> float:
        if price <= 0:
            return 0.0
        raw = self.cfg.alloc_usdt / float(price)
        # redondeo por step size
        step = await self.exchange.get_step_size(self.strategy.symbol)
        if step and step > 0:
            # floor al múltiplo de step
            import math
            raw = math.floor(raw / step) * step
        return max(raw, 0.0)

    def _stop_from_pct(self, side: str, ref_price: float) -> Optional[float]:
        pct = self.cfg.stop_pct
        if pct <= 0 or ref_price <= 0:
            return None
        if side == "LONG":
            return ref_price * (1.0 - pct)
        else:
            return ref_price * (1.0 + pct)

    def _fill_price(self, resp: Dict[str, Any], fallback: float) -> float:
        p = resp.get("avgPrice")
        if p:
            try:
                return float(p)
            except Exception:
                pass
        fills = resp.get("fills")
        if isinstance(fills, list) and fills:
            try:
                return float(fills[0].get("price"))
            except Exception:
                pass
        return float(fallback)

    # ---- core ----
    async def on_candle_close(self, close: float) -> None:
        # Nueva API: la estrategia actualiza internamente y devuelve la señal
        try:
            signal = self.strategy.generate_signal(close)   # <- correcto
        except TypeError:
            # Compat temporal con builds viejas
            self.strategy.update_candle(close)
            signal = self.strategy.generate_signal()

        if signal == "LONG":
            await self._ensure_long(close)
        elif signal == "SHORT":
            if self.cfg.allow_shorts:
                await self._ensure_short(close)
            else:
                await self._close_position(market_price=close)

    async def _ensure_long(self, close: float) -> None:
        # si ya estamos long, no-op; si está short → cerrar y girar
        if self.position and self.position.side == "LONG":
            return
        if self.position and self.position.side == "SHORT":
            await self._close_position(market_price=close)

        qty = await self._qty_from_alloc(price=close)
        if qty <= 0:
            return

        client_id = f"{self.cfg.bot_id}-{self.strategy.symbol}-{int(time.time()*1000)}"
        try:
            t0 = time.perf_counter()
            resp = await self.exchange.place_order(
                symbol=self.strategy.symbol,
                side="BUY",
                order_type="MARKET",
                quantity=qty,
                client_id=client_id,
            )
            observe_order_latency_ms((time.perf_counter() - t0) * 1000.0)
        except ExchangeError:
            inc_rejected_orders(1)
            return

        fill_price = self._fill_price(resp, close)
        stop_price = self._stop_from_pct("LONG", fill_price)
        stop_id: Optional[str] = None

        if stop_price:
            try:
                stop_resp = await self.exchange.place_order(
                    symbol=self.strategy.symbol,
                    side="SELL",
                    order_type="STOP_MARKET",
                    quantity=qty,
                    stop_price=stop_price,
                    client_id=f"{client_id}-SL",
                    reduce_only=True,
                )
                stop_id = str(stop_resp.get("orderId", "")) if isinstance(stop_resp, dict) else None
            except ExchangeError:
                # si no pudimos poner stop, igual quedamos LONG
                pass

        self.position = Position(side="LONG", qty=qty, entry_price=fill_price, stop_order_id=stop_id)

    async def _ensure_short(self, close: float) -> None:
        if self.position and self.position.side == "SHORT":
            return
        if self.position and self.position.side == "LONG":
            await self._close_position(market_price=close)

        qty = await self._qty_from_alloc(price=close)
        if qty <= 0:
            return

        client_id = f"{self.cfg.bot_id}-{self.strategy.symbol}-{int(time.time()*1000)}"
        try:
            t0 = time.perf_counter()
            resp = await self.exchange.place_order(
                symbol=self.strategy.symbol,
                side="SELL",
                order_type="MARKET",
                quantity=qty,
                client_id=client_id,
            )
            observe_order_latency_ms((time.perf_counter() - t0) * 1000.0)
        except ExchangeError:
            inc_rejected_orders(1)
            return

        fill_price = self._fill_price(resp, close)
        stop_price = self._stop_from_pct("SHORT", fill_price)
        stop_id: Optional[str] = None

        if stop_price:
            try:
                stop_resp = await self.exchange.place_order(
                    symbol=self.strategy.symbol,
                    side="BUY",
                    order_type="STOP_MARKET",
                    quantity=qty,
                    stop_price=stop_price,
                    client_id=f"{client_id}-SL",
                    reduce_only=True,
                )
                stop_id = str(stop_resp.get("orderId", "")) if isinstance(stop_resp, dict) else None
            except ExchangeError:
                pass

        self.position = Position(side="SHORT", qty=qty, entry_price=fill_price, stop_order_id=stop_id)

    async def _close_position(self, market_price: float) -> None:
        if not self.position:
            return
        pos = self.position
        side_close = "SELL" if pos.side == "LONG" else "BUY"

        # cancelar stop previo si lo hay
        try:
            if pos.stop_order_id:
                await self.exchange.cancel_order(self.strategy.symbol, order_id=pos.stop_order_id)
        except Exception:
            pass

        try:
            t0 = time.perf_counter()
            await self.exchange.place_order(
                symbol=self.strategy.symbol,
                side=side_close,
                order_type="MARKET",
                quantity=pos.qty,
                client_id=f"{self.cfg.bot_id}-exit-{int(time.time()*1000)}",
                reduce_only=True,
            )
            observe_order_latency_ms((time.perf_counter() - t0) * 1000.0)
        except ExchangeError:
            inc_rejected_orders(1)
        finally:
            self.position = None
