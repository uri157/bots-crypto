# runner/riskd.py
from __future__ import annotations

import asyncio
import logging
import os
import signal

import redis.asyncio as aioredis
import uvicorn
from fastapi import FastAPI

from metrics.server import register_metrics, bot_up
from riskd.guard import RiskGuardian

log = logging.getLogger("runner.riskd")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))


async def _run_http(app: FastAPI, host: str, port: int) -> None:
    cfg = uvicorn.Config(app, host=host, port=port, log_level=os.getenv("UVICORN_LOG_LEVEL", "info"))
    server = uvicorn.Server(cfg)
    await server.serve()


async def main() -> None:
    # --- ENV / Config ---
    redis_url = os.getenv("REDIS_URL", "redis://redis:6379/0")
    http_host = os.getenv("HTTP_HOST", "0.0.0.0")
    http_port = int(os.getenv("HTTP_PORT", "9004"))

    # DNS internos del compose o overrides
    momentum_host = os.getenv("MOMENTUM_HOST", "momentum")
    momentum_port = os.getenv("MOMENTUM_PORT", "9001")
    basis_host = os.getenv("BASIS_HOST", "basis")
    basis_port = os.getenv("BASIS_PORT", "9003")

    bots_api = {
        "momentum": f"http://{momentum_host}:{momentum_port}",
        "basis": f"http://{basis_host}:{basis_port}",
    }

    # --- Redis client ---
    redis = aioredis.from_url(redis_url, decode_responses=True)
    try:
        await redis.ping()
    except Exception as e:
        log.warning("No se pudo PING a Redis en inicio: %s", e)

    # --- Guardian ---
    guardian = RiskGuardian(redis, bots_api)

    # --- HTTP (metrics/health) ---
    app = FastAPI()
    register_metrics(app)
    http_task = asyncio.create_task(_run_http(app, host=http_host, port=http_port))
    bot_up.labels(bot="riskd").set(1)

    # --- Señales / parada ---
    stop_event = asyncio.Event()

    def _handle_sig():
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_sig)
        except NotImplementedError:
            pass

    # --- Monitor loop ---
    monitor_task = asyncio.create_task(guardian.monitor())

    log.info(
        "Risk Guardian iniciado | http=%s:%d momentum=%s basis=%s",
        http_host, http_port, bots_api["momentum"], bots_api["basis"]
    )

    try:
        await stop_event.wait()
    finally:
        log.info("Shutdown solicitado; cancelando tareas…")
        for t in (monitor_task, http_task):
            t.cancel()
        await asyncio.gather(monitor_task, http_task, return_exceptions=True)
        bot_up.labels(bot="riskd").set(0)
        try:
            await redis.close()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
