# datafeed/funding.py
from __future__ import annotations

import asyncio
from typing import List, Tuple, Optional

from redis.asyncio import Redis
from common.exchange import IExchange as Exchange  # contrato


async def schedule_funding_updates(
    exchange: Exchange,
    symbols: List[str],
    redis: Redis,
    interval_sec: int = 300,
    expire_sec: int = 1800,
) -> None:
    """
    Actualiza periódicamente el funding *actual* de cada símbolo en Redis.

    Claves:
      - funding:last:{SYMBOL}  -> tasa por período de 8h (decimal, p.ej. 0.0005 = 0.05%)
      - funding:{SYMBOL}       -> tasa *por hora* (decimal)  **USADA POR EXECUTOR**

    Args:
      exchange: adaptador del exchange.
      symbols: lista de símbolos perp (e.g., "BTCUSDT", "ETHUSDT").
      redis: cliente Redis asyncio.
      interval_sec: cada cuántos segundos refrescar.
      expire_sec: TTL de las claves en segundos.
    """
    while True:
        for sym in symbols:
            try:
                rate_8h = await exchange.get_funding_rate(sym)  # decimal por 8h
                # Normalizamos a tasa por hora (aprox. rate_8h / 8)
                rate_h = float(rate_8h) / 8.0
                await redis.set(f"funding:last:{sym}", str(rate_8h), ex=expire_sec)
                await redis.set(f"funding:{sym}", str(rate_h), ex=expire_sec)
            except Exception:
                # Silencioso: mantenemos el último valor válido
                pass
        await asyncio.sleep(max(5, int(interval_sec)))


def _percentile(sorted_vals: List[float], pct: float) -> float:
    """
    Percentil con interpolación lineal sobre lista ORDENADA.
    pct en [0,100]. Manejo robusto de bordes.
    """
    n = len(sorted_vals)
    if n == 0:
        return 0.0
    if pct <= 0:
        return float(sorted_vals[0])
    if pct >= 100:
        return float(sorted_vals[-1])

    pos = (pct / 100.0) * (n - 1)
    lo = int(pos)
    hi = min(lo + 1, n - 1)
    if lo == hi:
        return float(sorted_vals[lo])
    frac = pos - lo
    return float(sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac)


def compute_funding_percentiles(
    rates: List[float],
    long_pct: float,
    short_pct: float,
) -> Tuple[float, float]:
    """
    Calcula umbrales por percentiles a partir de tasas históricas (decimales).

    Returns:
      (max_long_rate, min_short_rate)
        - max_long_rate: LONG permitido sólo si funding <= este valor.
        - min_short_rate: SHORT permitido sólo si funding >= este valor.
    """
    if not rates:
        return (0.0, 0.0)
    sorted_rates = sorted(float(r) for r in rates)
    max_long_rate = _percentile(sorted_rates, long_pct)
    min_short_rate = _percentile(sorted_rates, short_pct)
    return (max_long_rate, min_short_rate)


async def refresh_and_store_funding_percentiles(
    exchange: Exchange,
    redis: Redis,
    symbol: str,
    lookback_days: int,
    long_pct: float,
    short_pct: float,
    key_prefix: str = "funding:pctl",
    expire_sec: int = 3600,
) -> Optional[Tuple[float, float]]:
    """
    Descarga historial de funding (~8 cortes/día * lookback_days), calcula percentiles
    y los guarda en Redis:
      - {key_prefix}:long:{symbol}  -> max_long_rate
      - {key_prefix}:short:{symbol} -> min_short_rate
    """
    try:
        hist = await exchange.get_historical_funding(symbol, lookback_days)
        max_long, min_short = compute_funding_percentiles(hist, long_pct, short_pct)
        await redis.set(f"{key_prefix}:long:{symbol}", str(max_long), ex=expire_sec)
        await redis.set(f"{key_prefix}:short:{symbol}", str(min_short), ex=expire_sec)
        return (max_long, min_short)
    except Exception:
        return None
