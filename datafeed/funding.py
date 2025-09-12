# datafeed/funding.py
from __future__ import annotations
import asyncio
import logging
from typing import Sequence, Dict, List

log = logging.getLogger("datafeed.funding")

async def schedule_funding_updates(symbols: Sequence[str], redis, interval_seconds: int = 60) -> None:
    """Stub: no-op. Mantiene vivo el task para no romper el runner."""
    log.info("Funding stub activo (no-op). symbols=%s", list(symbols))
    while True:
        await asyncio.sleep(interval_seconds)

def compute_funding_percentiles(history: List[float]) -> Dict[str, float]:
    """Stub: percentiles simples, o ceros si no hay datos."""
    if not history:
        return {"p50": 0.0, "p75": 0.0, "p90": 0.0}
    vals = sorted(float(x) for x in history)
    def pct(p: float) -> float:
        if not vals:
            return 0.0
        idx = min(int(p * (len(vals) - 1)), len(vals) - 1)
        return vals[idx]
    return {"p50": pct(0.50), "p75": pct(0.75), "p90": pct(0.90)}
