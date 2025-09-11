from typing import Optional
from .prices import PriceCache
from ..config import MomentumConfig

async def atr_fallback(symbol: str, prices: PriceCache, cfg: MomentumConfig, last_price_hint: float|None=None) -> Optional[float]:
    sym = (symbol or "").upper()
    pct = cfg.atr_min_pct_default
    if sym.startswith("BTC"):
        pct = cfg.atr_min_pct_btc
    elif sym.startswith("ETH"):
        pct = cfg.atr_min_pct_eth

    p = last_price_hint or await prices.get_last(symbol) or 0.0
    atr = pct * p
    return atr if atr > 0 else 1e-8
