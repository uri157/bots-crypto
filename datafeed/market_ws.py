# datafeed/market_ws.py
from __future__ import annotations

import asyncio
import json
import os
from typing import List, Optional

import aiohttp
from redis.asyncio import Redis


BINANCE_WS_BASE = os.getenv("BINANCE_WS_BASE", "wss://fstream.binance.com")
HEARTBEAT_SEC = int(os.getenv("WS_HEARTBEAT_SEC", "30"))
RECONNECT_MIN = float(os.getenv("WS_RECONNECT_MIN_SEC", "2"))
RECONNECT_MAX = float(os.getenv("WS_RECONNECT_MAX_SEC", "60"))
REDIS_TTL_SEC = int(os.getenv("MARKETDATA_TTL_SEC", "900"))  # 15 min por defecto


class MarketDataStreamer:
    """
    Suscripción WS a Binance Futures (USDⓈ-M) para:
      - Klines (múltiples intervalos)
      - Mark Price @1s
    Publica por Redis PUB/SUB y guarda últimos valores en claves con TTL.
    """

    def __init__(self, symbols: List[str], intervals: List[str], redis: Redis, include_mark_price: bool = True):
        self.symbols = [s.upper() for s in symbols]         # e.g., ["BTCUSDT", "ETHUSDT"]
        self.intervals = intervals                           # e.g., ["1h", "15m"]
        self.redis = redis
        self.include_mark_price = include_mark_price
        self._backoff = RECONNECT_MIN

    def _build_stream_url(self) -> str:
        """
        Construye URL combinada: /stream?streams=btcusdt@kline_1h/ethusdt@kline_15m/btcusdt@markPrice@1s/...
        """
        streams: List[str] = []
        for sym in self.symbols:
            for iv in self.intervals:
                streams.append(f"{sym.lower()}@kline_{iv}")
            if self.include_mark_price:
                streams.append(f"{sym.lower()}@markPrice@1s")
        stream_str = "/".join(streams)
        return f"{BINANCE_WS_BASE}/stream?streams={stream_str}"

    async def start(self) -> None:
        """
        Conecta y procesa indefinidamente, con reconexión exponencial.
        """
        while True:
            url = self._build_stream_url()
            try:
                timeout = aiohttp.ClientTimeout(total=None, connect=20, sock_read=None, sock_connect=20)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.ws_connect(url, heartbeat=HEARTBEAT_SEC, max_msg_size=0, autoping=True) as ws:
                        # reset backoff tras conexión exitosa
                        self._backoff = RECONNECT_MIN
                        await self._handle_stream(ws)
            except asyncio.CancelledError:
                raise
            except Exception:
                # reconexión con backoff
                await asyncio.sleep(self._backoff)
                self._backoff = min(self._backoff * 2, RECONNECT_MAX)

    async def _handle_stream(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except Exception:
                    continue
                await self._route_message(data)
            elif msg.type == aiohttp.WSMsgType.BINARY:
                # En Binance suele ser TEXT; ignoramos binario
                continue
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                break

    async def _route_message(self, packet: dict) -> None:
        """
        Paquete combinado: {"stream": "...", "data": {...}}
        Para klines: data = {"e":"kline","s":"BTCUSDT","k":{ ... "i":"1h","c":"xxxxx","x":true }}
        Para markPrice: data = {"e":"markPriceUpdate","s":"BTCUSDT","p":"xxxxx", ...}
        """
        data = packet.get("data")
        if not isinstance(data, dict):
            return

        evt = data.get("e")
        sym = data.get("s")

        # --- KLINE CLOSE ---
        if evt == "kline":
            k = data.get("k") or {}
            if not k:
                return
            interval = k.get("i")
            is_closed = bool(k.get("x"))
            close_price = k.get("c")
            if is_closed and sym and interval and close_price:
                # Guarda último candle cerrado y publica
                key_last = f"candle:last:{sym}:{interval}"
                chan = f"candles:{sym}:{interval}"
                try:
                    await self.redis.set(key_last, json.dumps(k), ex=REDIS_TTL_SEC)
                    await self.redis.publish(chan, json.dumps(k))
                except Exception:
                    pass
                # Actualiza también "price" con el close al cerrar la vela
                try:
                    await self.redis.set(f"price:last:{sym}", str(close_price), ex=REDIS_TTL_SEC)
                    await self.redis.publish(f"price:{sym}", str(close_price))
                except Exception:
                    pass
            return

        # --- MARK PRICE ---
        if evt == "markPriceUpdate" and sym:
            # 'p' = mark price
            mp = data.get("p")
            if mp is not None:
                try:
                    await self.redis.set(f"mark:last:{sym}", str(mp), ex=REDIS_TTL_SEC)
                    # también publicamos como "price" por compatibilidad de consumidores
                    await self.redis.set(f"price:last:{sym}", str(mp), ex=REDIS_TTL_SEC)
                    await self.redis.publish(f"price:{sym}", str(mp))
                except Exception:
                    pass
            return

        # Otros eventos se ignoran (aggTrades, miniTicker si algún día se agregan)
