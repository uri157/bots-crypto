from dataclasses import dataclass
import os

@dataclass(frozen=True)
class MomentumConfig:
    slippage_cap: float       = float(os.getenv("SLIPPAGE_CAP", "0.002"))
    loss_streak_limit: int    = int(os.getenv("LOSS_STREAK_LIMIT", "3"))
    no_trade_window_s: int    = int(os.getenv("NO_TRADE_FUNDING_WINDOW_S", "120"))
    order_timeout_ms: int     = int(os.getenv("ORDER_TIMEOUT_MS", "2000"))
    capital_lock_key: str     = os.getenv("CAPITAL_LOCK_KEY", "capital:lock:mm")

    atr_trail_mult: float     = float(os.getenv("ATR_TRAIL_MULT", "1.2"))
    atr_min_pct_default: float= float(os.getenv("ATR_MIN_PCT_DEFAULT", "0.005"))
    atr_min_pct_btc: float    = float(os.getenv("ATR_MIN_PCT_BTC", "0.008"))
    atr_min_pct_eth: float    = float(os.getenv("ATR_MIN_PCT_ETH", "0.010"))
