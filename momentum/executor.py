# momentum/executor.py (nuevo orquestador)
from __future__ import annotations
import time
from dataclasses import dataclass
from typing import Optional, Dict, Any
from redis.asyncio import Redis

from common.exchange import Exchange, ExchangeError
from metrics.server import observe_order_latency_ms, observe_slippage_bps, inc_rejected_orders

from .config import MomentumConfig
from .services.orders import OrderService
from .services.funding import FundingService
from .services.stops import StopManager
from .utils.prices import PriceCache
from .utils.atr import atr_fallback
from .utils.time_windows import within_no_trade_window

class MomentumStrategy:
    symbol: str
    def update_candle(self, close: float, high: float, low: float) -> None: ...
    def generate_signal(self, funding_rate_h: float, long_max: float, short_min: float) -> tuple[str, float, float]: ...
    def get_current_atr(self) -> Optional[float]: ...
    def get_last_close(self) -> Optional[float]: ...

@dataclass
class Position:
    side: str
    entry_price: float
    qty: float
    stop_price: float
    stop_order_id: Optional[str] = None

class MomentumExecutor:
    def __init__(self, exchange: Exchange, strategy: MomentumStrategy, redis: Redis, bot_id: str, cfg: Optional[MomentumConfig]=None):
        self.exchange = exchange
        self.strategy = strategy
        self.redis = redis
        self.bot_id = bot_id
        self.cfg = cfg or MomentumConfig()

        self.orders = OrderService(exchange)
        self.funding = FundingService(redis, use_percentiles=(os.getenv("USE_FUNDING_PERCENTILES","false").lower()=="true"))
        self.stops  = StopManager(self.orders)
        self.prices = PriceCache(redis)

        self.active_position: Optional[Position] = None
        self.state: str = "ACTIVE"
        self.consecutive_losses: int = 0
        self.last_trailing_ts_ms: int = 0
        self.last_order_ts_ms: int = 0

    # ----- hooks -----
    async def on_new_candle(self, close: float, high: float, low: float) -> None:
        if self.state == "PAUSED" or not await self._check_lock():
            return
        if within_no_trade_window(self.cfg.no_trade_window_s):
            return

        self.strategy.update_candle(close, high, low)

        f_rate = await self.funding.current_rate(self.strategy.symbol)
        long_max_env = float(os.getenv("FUNDING_LONG_MAX", "0.0"))
        short_min_env= float(os.getenv("FUNDING_SHORT_MIN", "0.0"))
        long_max, short_min = await self.funding.thresholds(self.strategy.symbol, long_max_env, short_min_env)

        signal, qty, stop_price = self.strategy.generate_signal(f_rate, long_max, short_min)
        if signal in ("LONG","SHORT") and self.active_position is None and qty > 0:
            await self._open_position(signal, qty, stop_price)

    async def on_price_update(self, last_price: float) -> None:
        if last_price > 0:
            self.prices.push_mem(self.strategy.symbol, last_price)
        if not self.active_position:
            return

        now_ms = int(time.time()*1000)
        if now_ms - self.last_trailing_ts_ms < 2000:
            return

        atr = self.strategy.get_current_atr() or await atr_fallback(self.strategy.symbol, self.prices, self.cfg, last_price)
        trail_dist = self.cfg.atr_trail_mult * atr

        pos = self.active_position
        if pos.side == "LONG":
            new_stop = max(pos.stop_price, last_price - trail_dist)
            if new_stop > pos.stop_price:
                await self._move_stop(new_stop, is_long=True)
        else:
            new_stop = min(pos.stop_price, last_price + trail_dist)
            if new_stop < pos.stop_price:
                await self._move_stop(new_stop, is_long=False)

        self.last_trailing_ts_ms = now_ms

    # ----- core -----
    async def _open_position(self, signal: str, quantity: float, stop_price: float) -> None:
        side = "BUY" if signal == "LONG" else "SELL"
        cid  = f"{self.bot_id}-{self.strategy.symbol}-{int(time.time()*1000)}"

        t0 = time.perf_counter()
        try:
            entry = await self.orders.market(self.strategy.symbol, side, quantity, cid)
        except ExchangeError:
            inc_rejected_orders(1); return
        finally:
            observe_order_latency_ms((time.perf_counter()-t0)*1000.0)

        fill_price = self._extract_fill_price(entry)
        ref_price  = (await self.prices.get_last(self.strategy.symbol)) or fill_price
        if ref_price > 0:
            slip = abs(fill_price - ref_price)/ref_price
            observe_slippage_bps(slip*1e4)
            if slip > self.cfg.slippage_cap:
                await self._rollback_entry(entry); inc_rejected_orders(1); return

        stop_cid = f"{cid}-SL"
        try:
            stop_id = await self.stops.place_initial(self.strategy.symbol, is_long=(signal=="LONG"),
                                                     qty=quantity, stop_price=stop_price, client_id=stop_cid)
        except ExchangeError:
            await self._rollback_entry(entry); inc_rejected_orders(1); return

        self.active_position = Position(signal, fill_price, quantity, stop_price, stop_id)
        self.last_order_ts_ms = int(time.time()*1000)

    async def _rollback_entry(self, entry: Dict[str,Any]) -> None:
        executed = float(entry.get("executedQty", 0.0))
        if executed > 0:
            opp = "SELL" if (entry.get("side") or "").upper()=="BUY" else "BUY"
            try:
                await self.orders.market(self.strategy.symbol, opp, executed, f"rollback-{entry.get('orderId')}-{int(time.time()*1000)}")
            except Exception:
                pass
        else:
            try:
                oid = entry.get("orderId")
                if oid: await self.orders.cancel(self.strategy.symbol, oid)
            except Exception:
                pass
        self.active_position = None

    async def _move_stop(self, new_stop_price: float, is_long: bool) -> None:
        if not self.active_position: return
        cid = f"trail-{self.bot_id}-{int(time.time()*1000)}"
        try:
            new_id = await self.stops.move(self.strategy.symbol, self.active_position.stop_order_id,
                                           is_long, self.active_position.qty, new_stop_price, cid)
            self.active_position.stop_order_id = new_id
            self.active_position.stop_price = new_stop_price
        except Exception:
            pass

    # ----- control -----
    async def _check_lock(self) -> bool:
        val = await self.redis.get(self.cfg.capital_lock_key)
        owner = val.decode() if isinstance(val,(bytes,bytearray)) else val
        if not owner or owner != self.bot_id:
            await self.pause_and_drain(); return False
        return True

    async def pause_and_drain(self) -> None:
        self.state = "PAUSED"
        if self.active_position:
            side_close = "SELL" if self.active_position.side == "LONG" else "BUY"
            try:
                await self.orders.market(self.strategy.symbol, side_close, self.active_position.qty, f"drain-{int(time.time()*1000)}")
            except Exception:
                pass
            self.active_position = None
        try:
            await self.redis.xadd("events", {"event":"drained","bot":self.bot_id})
        except Exception:
            pass

    def record_trade_result(self, pnl: float) -> None:
        self.consecutive_losses = self.consecutive_losses + 1 if pnl < 0 else 0
        if self.consecutive_losses >= self.cfg.loss_streak_limit:
            self.state = "PAUSED"

    # ----- helpers -----
    def _extract_fill_price(self, resp: Dict[str,Any]) -> float:
        if (p:=resp.get("avgPrice")): 
            try: return float(p)
            except: pass
        fills = resp.get("fills")
        if isinstance(fills,list) and fills:
            try: return float(fills[0].get("price"))
            except: pass
        return float(self.strategy.get_last_close() or 0.0)
