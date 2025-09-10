# runner/marketdata.py
from __future__ import annotations

import asyncio
import os
import signal
import time
import logging
from typing import List

import redis.asyncio as aioredis
import uvicorn
from fastapi import FastAPI

from common.exchange import Exchange
from datafeed.market_ws import MarketDataStreamer
from datafeed.funding import schedule_funding_updates, compute_funding_percentiles
from metrics.server import register_metrics, bot_up

log = logging.getLogger("runner.marketdata")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))


async def _run_http(app: FastAPI, host: str, port: int) -> None:
    cfg = uvicorn.Config(app, host=host, port=port, log_level=os.getenv("UVICORN_LOG_LEVEL", "info"))
    server = uvicorn.Server(cfg)
    await server.serve()


async def _funding_percentiles_loop(exchange: Exchange, redis, symbol: str) -> None:
    """
    (Opcional) Calcula percentiles globales de funding y los publica en Redis
    si USE_FUNDING_PERCENTILES=true. Escribe:
      - funding_long_max (umbral para permitir LONG)
      - funding_short_min (umbral para permitir SHORT)
    """
    use_pctl = os.getenv("USE_FUNDING_PERCENTILES", "false").lower() == "true"
    if not use_pctl:
        return

    lookback_days = int(os.getenv("FUNDING_PCTL_LOOKBACK_DAYS", "30"))
    p_long = float(os.getenv("FUNDING_LONG_MAX_PCTL", "40"))
    p_short = float(os.getenv("FUNDING_SHORT_MIN_PCTL", "60"))

    while True:
        try:
            rates = await exchange.get_historical_funding(symbol, days=lookback_days)  # lista de decimales
            max_long, min_short = compute_funding_percentiles(rates, p_long, p_short)
            await redis.set("funding_long_max", str(max_long), ex=3600)
            await redis.set("funding_short_min", str(min_short), ex=3600)
            log.info("Funding percentiles actualizados (%s): long<=%.6f short>=%.6f", symbol, max_long, min_short)
        except Exception as e:
            log.warning("No se pudieron actualizar percentiles de funding: %s", e)
        await asyncio.sleep(1800)  # cada 30 min


async def _price_heartbeat_from_pubsub(redis) -> None:
    """Actualiza metrics:last_price_ts cuando recibe cualquier mensaje de precio vía pub/sub."""
    ps = None
    try:
        ps = redis.pubsub()
        await ps.psubscribe("price:*")
        while True:
            msg = await ps.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if msg and msg.get("type") in ("pmessage", "message"):
                await redis.set("metrics:last_price_ts", str(time.time()), ex=120)
            await asyncio.sleep(0.01)
    except Exception as e:
        log.warning("price heartbeat pubsub caído: %s", e)
    finally:
        if ps is not None:
            try:
                await ps.close()
            except Exception:
                pass


async def main() -> None:
    # --- ENV / Config ---
    raw_symbols = os.getenv("SYMBOLS", "BTCUSDT,ETHUSDT")
    symbols: List[str] = [s.strip().upper() for s in raw_symbols.split(",") if s.strip()]
    if not symbols:
        symbols = ["BTCUSDT"]

    intervals = [os.getenv("TF_PRIMARY", "1h"), os.getenv("TF_SECONDARY", "15m")]
    host = os.getenv("HTTP_HOST", "0.0.0.0")
    http_port = int(os.getenv("HTTP_PORT", "9002"))
    mode = os.getenv("MODE", "paper").lower()

    # --- Redis ---
    redis_url = os.getenv("REDIS_URL", "redis://redis:6379/0")
    redis = aioredis.from_url(redis_url, decode_responses=True)
    try:
        await redis.ping()
    except Exception as e:
        log.warning("No se pudo PING a Redis en inicio: %s", e)

    # --- Streamer WS ---
    streamer = MarketDataStreamer(symbols, intervals, redis)

    # --- Exchange (para funding y utilidades) ---
    exchange = Exchange(
        os.getenv("BINANCE_API_KEY", ""),
        os.getenv("BINANCE_API_SECRET", ""),
        testnet=(mode == "paper"),
    )

    # --- Tareas concurrentes: funding actual + percentiles opcionales ---
    funding_task = asyncio.create_task(schedule_funding_updates(exchange, symbols, redis))
    pctl_task = asyncio.create_task(_funding_percentiles_loop(exchange, redis, symbols[0]))

    # --- HTTP (metrics/health) ---
    app = FastAPI()
    register_metrics(app)
    http_task = asyncio.create_task(_run_http(app, host=host, port=http_port))
    bot_up.labels(bot="marketdata").set(1)

    # --- Heartbeat de precios basado en pub/sub ---
    hb_task = asyncio.create_task(_price_heartbeat_from_pubsub(redis))

    # --- Señales para shutdown limpio ---
    stop_event = asyncio.Event()

    def _handle_sig():
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_sig)
        except NotImplementedError:
            pass

    log.info(
        "MarketData runner iniciado | symbols=%s intervals=%s http_port=%d mode=%s",
        symbols, intervals, http_port, mode
    )

    # --- Ejecutar WS streamer (reconecta internamente) hasta que pidan stop ---
    ws_task = asyncio.create_task(streamer.start())

    try:
        await stop_event.wait()
    finally:
        log.info("Shutdown solicitado; cancelando tareas…")
        for t in (ws_task, funding_task, pctl_task, hb_task, http_task):
            t.cancel()
        await asyncio.gather(ws_task, funding_task, pctl_task, hb_task, http_task, return_exceptions=True)
        bot_up.labels(bot="marketdata").set(0)
        try:
            await redis.close()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
