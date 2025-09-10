# runner/basis.py
from __future__ import annotations

import asyncio
import os
import signal
import logging
from typing import List, Dict, Any

import redis.asyncio as aioredis
import uvicorn
from fastapi import FastAPI

from common import locks
from common.exchange import Exchange
from basis.calc import BasisStrategy
from basis.executor import BasisExecutor
from metrics.server import register_metrics, bot_up
from control.api import create_control_api  # debe exponer /status /pause /resume /close_all /mode


log = logging.getLogger("runner.basis")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))


async def _run_http(app: FastAPI, host: str, port: int) -> None:
    cfg = uvicorn.Config(app, host=host, port=port, log_level=os.getenv("UVICORN_LOG_LEVEL", "info"))
    server = uvicorn.Server(cfg)
    await server.serve()


async def _renew_lock_loop(redis, key: str, holder: str, ttl: int, on_lost):
    """Renueva lock periódicamente; si se pierde, dispara pause+drain."""
    interval = max(10, min(ttl // 2, 60))  # 30–60s típico
    while True:
        try:
            ok = await locks.renew_lock(redis, key, holder, ttl=ttl)
            if not ok:
                log.warning("Capital lock perdido (%s). Pausando y drenando…", key)
                try:
                    await on_lost()
                except Exception as e:
                    log.error("Error en on_lost(): %s", e)
                # Intentar recuperarlo en background
                got = await locks.acquire_lock(redis, key, holder, ttl=ttl)
                if got:
                    log.info("Capital lock recuperado por %s", holder)
        except Exception as e:
            log.error("Error renovando lock: %s", e)
        await asyncio.sleep(interval)


async def _write_server_time_loop(redis):
    """Publica el server time de Binance para que riskd mida skew."""
    import aiohttp
    url = "https://fapi.binance.com/fapi/v1/time"
    while True:
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.get(url, timeout=3) as r:
                    data = await r.json()
                    if "serverTime" in data:
                        await redis.set("exchange:server_time_ms", str(float(data["serverTime"])), ex=5)
        except Exception:
            pass
        await asyncio.sleep(3)


async def main() -> None:
    # --- ENV / Config ---
    symbols_env = os.getenv("SYMBOLS", "BTCUSDT,ETHUSDT")
    base_assets: List[str] = [s.strip().upper().replace("USDT", "") for s in symbols_env.split(",") if s.strip()]
    equity = float(os.getenv("EQUITY_USDT", "100.0"))
    risk_pct = float(os.getenv("RISK_PCT_PER_TRADE", "0.01"))
    bot_id = os.getenv("BOT_ID", "basis")
    http_port = int(os.getenv("HTTP_PORT", "9003"))
    initial_mode = os.getenv("MODE", "paper").lower()
    lock_key = os.getenv("CAPITAL_LOCK_KEY", "capital:lock")
    lock_ttl = int(os.getenv("LOCK_TTL_SEC", "600"))
    poll_sec = int(os.getenv("BASIS_LOOP_INTERVAL_SEC", "60"))
    host = os.getenv("HTTP_HOST", "0.0.0.0")

    # --- Redis ---
    redis = aioredis.from_url(
        os.getenv("REDIS_URL", "redis://redis:6379/0"),
        decode_responses=True,
    )
    # Verificación rápida de conexión (no fatal si falla, pero útil para logs)
    try:
        await redis.ping()
    except Exception as e:
        log.warning("No se pudo PING a Redis en inicio: %s", e)

    # --- Lock inicial (no bloqueante) ---
    got_lock = await locks.acquire_lock(redis, lock_key, bot_id, ttl=lock_ttl)
    if not got_lock:
        log.warning("Basis bot (%s) no obtuvo lock '%s'; inicia en pausa y drenaje si aplica.", bot_id, lock_key)

    # --- Exchange ---
    exchange = Exchange(
        os.getenv("BINANCE_API_KEY", ""),
        os.getenv("BINANCE_API_SECRET", ""),
        testnet=(initial_mode == "paper"),
    )

    # --- Estrategia + Executor (1 activo principal por MVP) ---
    base = base_assets[0] if base_assets else "BTC"
    strategy = BasisStrategy(base, exchange)
    executor = BasisExecutor(exchange, strategy, redis, bot_id)

    # --- Estado expuesto por la API de control ---
    _state: Dict[str, Any] = {"mode": initial_mode, "base": base}

    def _status_payload() -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "ok": True,
            "bot": bot_id,
            "module": "basis",
            "bases": base_assets,
            "base": _state["base"],
            "mode": _state["mode"],
            "equity_usdt": equity,
            "risk_pct_per_trade": risk_pct,
        }
        # Datos opcionales del executor si existen de forma segura/síncrona
        for attr in ("active_position", "spread", "last_update"):
            if hasattr(executor, attr):
                try:
                    payload[attr] = getattr(executor, attr)
                except Exception:
                    pass
        return payload

    def _get_mode() -> str:
        return str(_state["mode"])

    def _set_mode(m: str) -> None:
        _state["mode"] = str(m or "").lower()

    # No-ops seguros por ahora (podemos cablear a métodos async luego)
    def _pause() -> None:
        log.info("pause() solicitado vía API (no-op síncrono).")

    def _resume() -> None:
        log.info("resume() solicitado vía API (no-op síncrono).")

    def _close_all() -> None:
        log.info("close_all() solicitado vía API (no-op síncrono).")

    # --- API control + métricas ---
    app = create_control_api(
        status=_status_payload,
        pause=_pause,
        resume=_resume,
        close_all=_close_all,
        get_mode=_get_mode,
        set_mode=_set_mode,
    )
    register_metrics(app)
    bot_up.labels(bot=bot_id).set(1)

    # --- Tareas concurrentes ---
    http_task = asyncio.create_task(_run_http(app, host=host, port=http_port))
    lock_task = asyncio.create_task(_renew_lock_loop(redis, lock_key, bot_id, lock_ttl, executor.pause_and_drain))
    time_task = asyncio.create_task(_write_server_time_loop(redis))

    # --- Señales de parada limpia ---
    stop_event = asyncio.Event()

    def _handle_sig():
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_sig)
        except NotImplementedError:
            pass  # en Windows o entornos restringidos

    log.info(
        "Basis runner iniciado | bot_id=%s base=%s http_port=%d mode=%s",
        bot_id, _state["base"], http_port, _state["mode"]
    )

    # --- Bucle de trading ---
    try:
        while not stop_event.is_set():
            try:
                await executor.try_open_trade()
                await executor.monitor_trade()
            except Exception as e:
                log.error("Error en loop de trading: %s", e)
            await asyncio.sleep(poll_sec)
    finally:
        log.info("Shutdown solicitado; drenando posiciones…")
        try:
            await executor.pause_and_drain()
        except Exception:
            pass
        for t in (http_task, lock_task, time_task):
            t.cancel()
        await asyncio.gather(http_task, lock_task, time_task, return_exceptions=True)
        bot_up.labels(bot=bot_id).set(0)
        try:
            await locks.release_lock(redis, lock_key, bot_id)
        except Exception:
            pass
        try:
            await redis.close()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
