from typing import Optional
from redis.asyncio import Redis

class PriceCache:
    def __init__(self, redis: Redis):
        self.redis = redis
        self._mem: dict[str, float] = {}

    async def get_last(self, symbol: str) -> Optional[float]:
        raw = await self.redis.get(f"price:last:{symbol}")
        if raw is None:
            return self._mem.get(symbol)
        try:
            v = float(raw.decode() if isinstance(raw, (bytes, bytearray)) else raw)
            self._mem[symbol] = v
            return v
        except Exception:
            return self._mem.get(symbol)

    def push_mem(self, symbol: str, price: float) -> None:
        if price > 0:
            self._mem[symbol] = price
