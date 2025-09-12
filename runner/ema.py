# runner/ema.py
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Optional, Awaitable, Callable

import uvicorn
from fastapi import FastAPI, Response
from redis.asyncio import Redis

from common.exchange import Exchange
from ema.config import EmaConfig
from ema.strategy import EmaCrossStrategy
from ema.executor import EmaExecutor

# ── logging ──────────────────────────────────────────────────────────────────────
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("runner.ema")

# ── Métricas: intentamos usar metrics.server; si no, registry local ─────────────
USE_EXTERNAL_METRICS = False
REG = None  # type: ignore
BOT_UP = None  # type: ignore
ORDER_LAT_MS = None  # type: ignore

# Funciones que el executor puede llamar (defaults no-op si no hay metrics.server)
def start_metrics_http(_port: int, _bot: str) -> None:  # default no-op
    ...

def observe_order_latency_ms(ms: float) -> None:  # default no-op
    ...

def inc_rejected_orders(n: int) -> None:  # default no-op
    ...

try:
    # Si existe un servidor de métricas común para todos los servicios, lo usamos.
    from metrics.server import (  # type: ignore
        start_metrics_http as _start_metrics_http,
        observe_order_latency_ms as _observe_order_latency_ms,
        inc_rejected_orders as _inc_rejected_orders,
        REG as _GLOBAL_REG,
        BOT_UP as _GLOBAL_BOT_UP,
    )

    start_metrics_http = _start_metrics_http
    observe_order_latency_ms = _observe_order_latency_ms
    inc_rejected_orders = _inc_rejected_orders
    REG = _GLOBAL_REG
    BOT_UP = _GLOBAL_BOT_UP
    USE_EXTERNAL_METRICS = True
    log.info("Métricas: usando metrics.server (registry global)")
except Exception:
    # Fallback: registry propia en este proceso para evitar colisiones
    from prometheus_client import CollectorRegistry, Gauge, Histogram

    REG = CollectorRegistry()
    BOT_UP = Gauge("bot_up", "Bot process running (1=up)", ["bot"], registry=REG)
    ORDER_LAT_MS = Histogram(
        "order_latency_ms",
        "Latency of order executions in ms",
        buckets=[5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, float("inf")],
        registry=REG,
    )

    def start_metrics_http(_port: int, _bot: str) -> None:
        # No iniciamos nada extra en el fallback
        ...

    def observe_order_latency_ms(ms: float) -> None:
        ORDER_LAT_MS.observe(ms)  # type: ignore

    def inc_rejected_orders(n: int) -> None:
        ...

    log.info("Métricas: usando registry local (fallback)")

# ── helpers ─────────────────────────────────────────────────────────────────────
def set_bot_up(bot_id: str) -> None:
    try:
        if BOT_UP is not None:
            BOT_UP.labels(bot=bot_id).set(1.0)
    except Exception:
        pass


# ── http app (healthz + /metrics + info) ────────────────────────────────────────
def build_http_app(bot_id: str) -> FastAPI:
    app = FastAPI()

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/metrics")
    def metrics():
        # Import local para evitar problemas de alcance en distintos paths
        from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
        return Response(generate_latest(REG), media_type=CONTENT_TYPE_LATEST)  # type: ignore[arg-type]

    @app.get("/info")
    def info():
        return {"bot": bot_id, "strategy": "ema_cross"}

    return app


# ── loop de datos: lee última vela cerrada desde Redis ──────────────────────────
async def candle_poll_loop(
    cfg: EmaConfig,
    redis: Redis,
    on_closed_candle: Callable[[float], Awaitable[None]],
) -> None:
    """
    Lee 'candle:last:{SYMBOL}:{TF}' de Redis y dispara on_closed_candle(close) cuando cambia.
    Formato esperado del marketdata: {t, T, s, i, o, c, h, l, x: bool}
    """
    key = f"candle:last:{cfg.symbol}:{cfg.tf_primary}"
    last_t: Optional[int] = None
    log.info("Poll de velas: key=%s", key)
    while True:
        try:
            raw = await redis.get(key)
            if raw:
                try:
                    obj = json.loads(raw.decode() if isinstance(raw, (bytes, bytearray)) else raw)
                    # x=True cuando cierra la vela
                    if obj.get("x") is True:
                        t = int(obj.get("t", 0))
                        if last_t is None or t != last_t:
                            last_t = t
                            close = float(obj.get("c", 0.0))
                            await on_closed_candle(close)
                except Exception as e:
                    log.warning("Error parseando candle: %s", e)
        except Exception as e:
            log.warning("Redis get %s: %s", key, e)
        await asyncio.sleep(0.2)


# ── main ────────────────────────────────────────────────────────────────────────
async def main():
    cfg = EmaConfig()

    # HTTP server (FastAPI)
    app = build_http_app(cfg.bot_id)
    config = uvicorn.Config(app=app, host="0.0.0.0", port=cfg.http_port, log_level="info")
    server = uvicorn.Server(config)
    http_task = asyncio.create_task(server.serve())

    # Inicia (si existe) el servidor de métricas externo
    try:
        start_metrics_http(cfg.http_port, cfg.bot_id)
    except Exception:
        pass

    # Marca el bot "up" en la registry disponible
    set_bot_up(cfg.bot_id)

    # Redis
    redis = Redis.from_url(cfg.redis_url, decode_responses=False)

    # Exchange & Strategy
    ex = Exchange(cfg.binance_api_key, cfg.binance_api_secret, testnet=cfg.testnet)
    strat = EmaCrossStrategy(symbol=cfg.symbol, fast=cfg.ema_fast, slow=cfg.ema_slow)
    execu = EmaExecutor(exchange=ex, strategy=strat, cfg=cfg)

    async def _on_closed(close: float):
        await execu.on_candle_close(close)

    # Datos
    poll_task = asyncio.create_task(candle_poll_loop(cfg, redis, _on_closed))

    log.info(
        "EMA runner listo. symbol=%s tf=%s fast=%d slow=%d alloc_usdt=%.2f stop_pct=%.4f allow_shorts=%s",
        cfg.symbol,
        cfg.tf_primary,
        cfg.ema_fast,
        cfg.ema_slow,
        cfg.alloc_usdt,
        cfg.stop_pct,
        cfg.allow_shorts,
    )

    # Mantener procesos
    await asyncio.gather(http_task, poll_task)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
