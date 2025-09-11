from typing import Tuple
from redis.asyncio import Redis

class FundingService:
    def __init__(self, redis: Redis, use_percentiles: bool):
        self.redis = redis
        self.use_percentiles = use_percentiles

    async def current_rate(self, symbol: str) -> float:
        raw = await self.redis.get(f"funding:{symbol}")
        return float(raw.decode() if isinstance(raw, (bytes, bytearray)) else (raw or 0.0))

    async def thresholds(self, symbol: str, long_max_env: float, short_min_env: float) -> Tuple[float,float]:
        if not self.use_percentiles:
            return long_max_env, short_min_env
        p_long = await self.redis.get(f"funding:pctl:long:{symbol}")
        p_short = await self.redis.get(f"funding:pctl:short:{symbol}")
        long_max = float(p_long.decode() if isinstance(p_long,(bytes,bytearray)) else p_long) if p_long is not None else long_max_env
        short_min = float(p_short.decode() if isinstance(p_short,(bytes,bytearray)) else p_short) if p_short is not None else short_min_env
        return long_max, short_min
