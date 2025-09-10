# runner/momentum.py
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from typing import List, Dict, Any

import redis.asyncio as aioredis
import uvicorn
from fastapi import FastAPI

from common import locks
from common.exchange import Exchange
from control.api import create_control_api  # expone /status /pause /resume /close_all /mode
from metrics.server import register_metrics, bot_up
from momentum.strategy import MomentumStrategy
from momentum.executor import MomentumExecutor


log = logging.getLogger("runner.momentum")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))


def _base_from_symbol(sym: str) -> str:
    """BTCUSDT -> BTC"""
    s = sym.strip().upper()
    return s[:-4] if s.endswith("USDT") else s


async def _run_http(app: FastAPI, host: str, port: int) -> None:
    cfg = uvicorn.Config(app, host=host, port=port, log_level=os.getenv("UVICORN_LOG_LEVEL", "info"))
    server = uvicorn.Server(cfg)
    await server.serve()


async def _renew_lock_loop(redis, key: str, holder: str, ttl: int, on_lost):
    """Renueva el capital lock periódicamente; si se pierde, pausa y drena."""
    interval = max(10, min(ttl // 2, 60))
    while True:
        try:
            ok = await locks.renew_lock(redis, key, holder, ttl=ttl)
            if not ok:
                log.warning("Capital lock perdido (%s). Pausando y drenando…", key)
                # Intento de mitigación: drenar posiciones
                try:
                    await on_lost()
                except Exception as e:
                    log.error("Error en on_lost(): %s", e)
                got = await locks.acquire_lock(redis, key, holder, ttl=ttl)
                if got:
                    log.info("Capital lock recuperado por %s", holder)
        except Exception as e:
            log.error("Error renovando lock: %s", e)
        await asyncio.sleep(interval)


async def _pubsub_loop(redis, symbol: str, executor: MomentumExecutor, stop_event: asyncio.Event):
    """Escucha velas cerradas 1h y ticks de precio; dispara callbacks del executor."""
    ps = redis.pubsub()
    chan_candle = f"candles:{symbol}:1h"
    chan_price = f"price:{symbol}"
    await ps.subscribe(chan_candle, chan_price)
    log.info("Suscripto a %s y %s", chan_candle, chan_price)

    try:
        while not stop_event.is_set():
            msg = await ps.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if not msg:
                await asyncio.sleep(0.01)
                continue

            if msg.get("type") not in ("message", "pmessage"):
                continue

            channel = msg.get("channel")
            data = msg.get("data")

            # Heartbeat para riskd (downtime WS) usando epoch seconds
            await redis.set("metrics:last_price_ts", str(time.time()), ex=120)

            if channel == chan_candle:
                try:
                    payload = json.loads(data)
                    close_price = float(payload["c"])
                    high = float(payload["h"])
                    low = float(payload["l"])
                    await executor.on_new_candle(close_price, high, low)
                except Exception as e:
                    log.error("Error procesando candle: %s", e)
            elif channel == chan_price and getattr(executor, "active_position", None):
                try:
                    price = float(data)
                    await executor.on_price_update(price)
                except Exception as e:
                    log.error("Error procesando precio: %s", e)
    finally:
        try:
            await ps.unsubscribe(chan_candle, chan_price)
            await ps.close()
        except Exception:
            pass


async def main() -> None:
    # --- ENV / Config ---
    raw_symbols = os.getenv("SYMBOLS", "BTCUSDT")
    symbols: List[str] = [s.strip().upper() for s in raw_symbols.split(",") if s.strip()]
    symbol = symbols[0]  # MVP: un símbolo principal
    base = _base_from_symbol(symbol)

    equity = float(os.getenv("EQUITY_USDT", "100.0"))
    risk_pct = float(os.getenv("RISK_PCT_PER_TRADE", "0.01"))

    # ATR_MIN_PCT_BTC / ATR_MIN_PCT_ETH por base
    atr_min_pct_env = os.getenv(f"ATR_MIN_PCT_{base}", os.getenv("ATR_MIN_PCT_DEFAULT", "0.0"))
    atr_min_pct = float(atr_min_pct_env)

    bot_id = os.getenv("BOT_ID", "momentum")
    initial_mode = os.getenv("MODE", "paper").lower()
    http_port = int(os.getenv("HTTP_PORT", "9001"))
    host = os.getenv("HTTP_HOST", "0.0.0.0")

    lock_key = os.getenv("CAPITAL_LOCK_KEY", "capital:lock")
    lock_ttl = int(os.getenv("LOCK_TTL_SEC", "600"))

    # --- Redis ---
    redis_url = os.getenv("REDIS_URL", "redis://redis:6379/0")
    redis = aioredis.from_url(redis_url, decode_responses=True)
    try:
        await redis.ping()
    except Exception as e:
        log.warning("No se pudo PING a Redis: %s", e)

    # --- Capital lock inicial (no bloqueante) ---
    got_lock = await locks.acquire_lock(redis, lock_key, bot_id, ttl=lock_ttl)
    if not got_lock:
        log.warning("Momentum bot (%s) no obtuvo lock '%s'; inicia en PAUSED y drena si aplica.", bot_id, lock_key)

    # --- Exchange ---
    exchange = Exchange(
        os.getenv("BINANCE_API_KEY", ""),
        os.getenv("BINANCE_API_SECRET", ""),
        testnet=(initial_mode == "paper"),
    )

    # --- Estrategia + Executor ---
    strategy = MomentumStrategy(
        symbol=symbol,
        equity=equity,
        risk_pct=risk_pct,
        atr_min_pct=atr_min_pct,
        atr_period=14,
        ema_fast=50,
        ema_slow=200,
        adx_min=20.0,
    )
    executor = MomentumExecutor(exchange, strategy, redis, bot_id)

    # --- Estado expuesto por la API de control ---
    # Usamos un contenedor mutable para mode (capturable por lambdas sync)
    _state: Dict[str, Any] = {"mode": initial_mode}

    def _status_payload() -> Dict[str, Any]:
        # Ajustá acá lo que quieras ver en /status
        payload: Dict[str, Any] = {
            "ok": True,
            "bot": bot_id,
            "module": "momentum",
            "symbol": symbol,
            "base": base,
            "mode": _state["mode"],
            "equity_usdt": equity,
            "risk_pct_per_trade": risk_pct,
        }
        # Datos opcionales del executor si existen de forma segura/síncrona
        for attr in ("active_position", "last_signal", "open_orders"):
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

    # No-ops seguros (los reales suelen ser async; podemos cablearlos luego con fondo)
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
    http_task = asyncio.create_task(_run_http(app, host=host, port=http_port))
    bot_up.labels(bot=bot_id).set(1)

    # --- Lock renewer ---
    lock_task = asyncio.create_task(_renew_lock_loop(redis, lock_key, bot_id, lock_ttl, executor.pause_and_drain))

    # --- Pub/Sub loop ---
    stop_event = asyncio.Event()
    ps_task = asyncio.create_task(_pubsub_loop(redis, symbol, executor, stop_event))

    # --- Señales para shutdown limpio ---
    def _handle_sig():
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_sig)
        except NotImplementedError:
            pass

    log.info(
        "Momentum runner iniciado | symbol=%s base=%s http_port=%d mode=%s",
        symbol, base, http_port, _state["mode"]
    )

    # Esperar parada
    try:
        await stop_event.wait()
    finally:
        log.info("Shutdown solicitado; drenando posición y cerrando tareas…")
        try:
            await executor.pause_and_drain()
        except Exception:
            pass

        for t in (ps_task, lock_task, http_task):
            t.cancel()
        await asyncio.gather(ps_task, lock_task, http_task, return_exceptions=True)

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
