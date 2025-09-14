# ema/executor.py
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional, Dict, Any, List

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
    Ejecutor simple + persistencia:
      - En cruce LONG abre BUY MARKET por 'alloc_usdt / price'
      - En cruce opuesto cierra posición (y opcionalmente abre SHORT si allow_shorts)
      - Stop-loss fijo (STOP_PCT) vía STOP_MARKET reduceOnly (si STOP_PCT > 0)
      - **Persistencia**: orders / fills (si hay DB disponible)
    """
    def __init__(self, exchange: Exchange, strategy: EmaCrossStrategy, cfg: EmaConfig) -> None:
        self.exchange = exchange
        self.strategy = strategy
        self.cfg = cfg
        self.position: Optional[Position] = None

        # Inyectados por runner. Si no están, la persistencia queda no-op.
        self.db_conn = getattr(self, "db_conn", None)   # psycopg connection (set por runner)
        self.run_id: Optional[str] = getattr(self, "run_id", None)
        self.cfg_id: Optional[int] = getattr(self, "cfg_id", None)

    # --------------------------- DB helpers ---------------------------

    @property
    def _db_ready(self) -> bool:
        return self.db_conn is not None and self.run_id is not None

    def _db_insert_order(
        self,
        *,
        client_id: str,
        venue_order_id: Optional[str],
        side: str,
        order_type: str,
        status: str,
        quantity: float,
        price: Optional[float] = None,
        stop_price: Optional[float] = None,
        reduce_only: Optional[bool] = None,
        time_in_force: Optional[str] = None,
        created_ts_ms: Optional[int] = None,
        exchange_ts_ms: Optional[int] = None,
        leverage: Optional[int] = None,
        quote_qty: Optional[float] = None,
    ) -> Optional[int]:
        """Inserta/actualiza una orden y devuelve order_uid (o None si no hay DB)."""
        if not self._db_ready:
            return None
        con = self.db_conn
        assert con is not None
        try:
            with con.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO orders (
                        run_id, bot_id, instance_id, environment, venue, symbol,
                        client_order_id, venue_order_id,
                        side, order_type, status, time_in_force, reduce_only,
                        price, stop_price, quantity, quote_qty, leverage,
                        created_ts_ms, exchange_ts_ms, created_at
                    )
                    VALUES (
                        %(run_id)s, %(bot_id)s, %(instance_id)s, %(environment)s, %(venue)s, %(symbol)s,
                        %(client_id)s, %(venue_order_id)s,
                        %(side)s, %(order_type)s, %(status)s, %(tif)s, %(reduce_only)s,
                        %(price)s, %(stop_price)s, %(qty)s, %(quote_qty)s, %(leverage)s,
                        %(created_ts_ms)s, %(exchange_ts_ms)s, NOW()
                    )
                    ON CONFLICT (venue, client_order_id) DO UPDATE
                      SET status = EXCLUDED.status,
                          venue_order_id = COALESCE(orders.venue_order_id, EXCLUDED.venue_order_id),
                          exchange_ts_ms = COALESCE(orders.exchange_ts_ms, EXCLUDED.exchange_ts_ms)
                    RETURNING order_uid;
                    """,
                    dict(
                        run_id=self.run_id,
                        bot_id=self.cfg.bot_id,
                        instance_id=getattr(self.cfg, "instance_id", "unknown"),
                        environment=getattr(self.cfg, "run_env", "sim"),
                        venue=getattr(self.cfg, "venue", "binance-sim"),
                        symbol=self.strategy.symbol,
                        client_id=client_id,
                        venue_order_id=str(venue_order_id) if venue_order_id else None,
                        side=side,
                        order_type=order_type,
                        status=status,
                        tif=time_in_force,
                        reduce_only=reduce_only,
                        price=price,
                        stop_price=stop_price,
                        qty=quantity,
                        quote_qty=quote_qty,
                        leverage=leverage,
                        created_ts_ms=created_ts_ms or int(time.time() * 1000),
                        exchange_ts_ms=exchange_ts_ms,
                    ),
                )
                row = cur.fetchone()
            con.commit()
            return int(row[0]) if row else None
        except Exception:
            try:
                con.rollback()
            except Exception:
                pass
            return None

    def _db_insert_fills(
        self,
        *,
        order_uid: Optional[int],
        side: str,
        exchange_ts_ms: Optional[int],
        fills: Optional[List[Dict]],
    ) -> None:
        """Inserta fills (si hay DB). Realized PnL queda en 0.0 (lo calcula análisis)."""
        if not self._db_ready or not order_uid or not fills:
            return
        con = self.db_conn
        assert con is not None
        try:
            with con.cursor() as cur:
                for i, f in enumerate(fills, start=1):
                    price = float(f.get("price", 0.0))
                    # Binance a veces devuelve 'qty' (FUTURES) o 'executedQty'
                    qty = float(f.get("qty", f.get("executedQty", 0.0)) or 0.0)
                    fee = float(f.get("commission", 0.0))
                    fee_asset = str(f.get("commissionAsset", "USDT"))
                    is_maker = bool(f.get("isMaker", False))
                    quote_qty = price * qty if price and qty else None
                    cur.execute(
                        """
                        INSERT INTO fills (
                            order_uid, run_id, seq, ts_ms, price, qty, quote_qty,
                            fee, fee_asset, is_maker, realized_pnl, side
                        )
                        VALUES (
                            %(order_uid)s, %(run_id)s, %(seq)s, %(ts_ms)s, %(price)s, %(qty)s, %(quote_qty)s,
                            %(fee)s, %(fee_asset)s, %(is_maker)s, 0.0, %(side)s
                        )
                        ON CONFLICT (order_uid, seq) DO NOTHING;
                        """,
                        dict(
                            order_uid=order_uid,
                            run_id=self.run_id,
                            seq=i,
                            ts_ms=exchange_ts_ms or int(time.time() * 1000),
                            price=price,
                            qty=qty,
                            quote_qty=quote_qty,
                            fee=fee,
                            fee_asset=fee_asset,
                            is_maker=is_maker,
                            side=side,
                        ),
                    )
            con.commit()
        except Exception:
            try:
                con.rollback()
            except Exception:
                pass

    def _db_mark_canceled_by_venue_id(self, venue_order_id: Optional[str]) -> None:
        """Marca una orden como CANCELED usando venue_order_id (ej: al cancelar un STOP)."""
        if not self._db_ready or not venue_order_id:
            return
        con = self.db_conn
        assert con is not None
        try:
            with con.cursor() as cur:
                cur.execute(
                    """
                    UPDATE orders
                    SET status = 'CANCELED'
                    WHERE run_id = %(run_id)s
                      AND venue = %(venue)s
                      AND venue_order_id = %(venue_order_id)s;
                    """,
                    dict(
                        run_id=self.run_id,
                        venue=getattr(self.cfg, "venue", "binance-sim"),
                        venue_order_id=str(venue_order_id),
                    ),
                )
            con.commit()
        except Exception:
            try:
                con.rollback()
            except Exception:
                pass

    # --------------------------- sizing & stops ---------------------------

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

    # --------------------------- core loop ---------------------------

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

        # Persistir orden MARKET + fills
        venue_order_id = str(resp.get("orderId")) if resp.get("orderId") else None
        exch_ts = int(resp.get("transactTime")) if resp.get("transactTime") else None
        order_uid = self._db_insert_order(
            client_id=client_id,
            venue_order_id=venue_order_id,
            side="BUY",
            order_type="MARKET",
            status=str(resp.get("status", "FILLED")),
            quantity=qty,
            price=None,
            stop_price=None,
            reduce_only=False,
            time_in_force=resp.get("timeInForce"),
            created_ts_ms=int(time.time() * 1000),
            exchange_ts_ms=exch_ts,
            quote_qty=None,
            leverage=None,
        )
        self._db_insert_fills(
            order_uid=order_uid,
            side="BUY",
            exchange_ts_ms=exch_ts,
            fills=resp.get("fills"),
        )

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

                # Persistir STOP (status NEW, sin fills)
                self._db_insert_order(
                    client_id=f"{client_id}-SL",
                    venue_order_id=stop_id,
                    side="SELL",
                    order_type="STOP_MARKET",
                    status=str(stop_resp.get("status", "NEW")),
                    quantity=qty,
                    price=None,
                    stop_price=stop_price,
                    reduce_only=True,
                    time_in_force=stop_resp.get("timeInForce"),
                    created_ts_ms=int(time.time() * 1000),
                    exchange_ts_ms=int(stop_resp.get("transactTime")) if stop_resp.get("transactTime") else None,
                )
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

        # Persistir orden MARKET + fills
        venue_order_id = str(resp.get("orderId")) if resp.get("orderId") else None
        exch_ts = int(resp.get("transactTime")) if resp.get("transactTime") else None
        order_uid = self._db_insert_order(
            client_id=client_id,
            venue_order_id=venue_order_id,
            side="SELL",
            order_type="MARKET",
            status=str(resp.get("status", "FILLED")),
            quantity=qty,
            price=None,
            stop_price=None,
            reduce_only=False,
            time_in_force=resp.get("timeInForce"),
            created_ts_ms=int(time.time() * 1000),
            exchange_ts_ms=exch_ts,
            quote_qty=None,
            leverage=None,
        )
        self._db_insert_fills(
            order_uid=order_uid,
            side="SELL",
            exchange_ts_ms=exch_ts,
            fills=resp.get("fills"),
        )

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

                # Persistir STOP (status NEW, sin fills)
                self._db_insert_order(
                    client_id=f"{client_id}-SL",
                    venue_order_id=stop_id,
                    side="BUY",
                    order_type="STOP_MARKET",
                    status=str(stop_resp.get("status", "NEW")),
                    quantity=qty,
                    price=None,
                    stop_price=stop_price,
                    reduce_only=True,
                    time_in_force=stop_resp.get("timeInForce"),
                    created_ts_ms=int(time.time() * 1000),
                    exchange_ts_ms=int(stop_resp.get("transactTime")) if stop_resp.get("transactTime") else None,
                )
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
                # Reflejar en DB (si estaba insertado)
                self._db_mark_canceled_by_venue_id(pos.stop_order_id)
        except Exception:
            pass

        client_id = f"{self.cfg.bot_id}-exit-{int(time.time()*1000)}"
        try:
            t0 = time.perf_counter()
            resp = await self.exchange.place_order(
                symbol=self.strategy.symbol,
                side=side_close,
                order_type="MARKET",
                quantity=pos.qty,
                client_id=client_id,
                reduce_only=True,
            )
            observe_order_latency_ms((time.perf_counter() - t0) * 1000.0)
        except ExchangeError:
            inc_rejected_orders(1)
            return
        finally:
            self.position = None

        # Persistir orden de cierre + fills
        venue_order_id = str(resp.get("orderId")) if resp.get("orderId") else None
        exch_ts = int(resp.get("transactTime")) if resp.get("transactTime") else None
        order_uid = self._db_insert_order(
            client_id=client_id,
            venue_order_id=venue_order_id,
            side=side_close,
            order_type="MARKET",
            status=str(resp.get("status", "FILLED")),
            quantity=pos.qty,
            price=None,
            stop_price=None,
            reduce_only=True,
            time_in_force=resp.get("timeInForce"),
            created_ts_ms=int(time.time() * 1000),
            exchange_ts_ms=exch_ts,
            quote_qty=None,
            leverage=None,
        )
        self._db_insert_fills(
            order_uid=order_uid,
            side=side_close,
            exchange_ts_ms=exch_ts,
            fills=resp.get("fills"),
        )
