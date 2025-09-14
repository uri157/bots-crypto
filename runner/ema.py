# runner/ema.py
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import uuid
import hashlib
from typing import Optional, Awaitable, Callable

import uvicorn
from fastapi import FastAPI, Response
from redis.asyncio import Redis

# DB (psycopg v3; si usas psycopg2 cambia los imports a psycopg2)
try:
    import psycopg  # type: ignore
except Exception:  # pragma: no cover
    psycopg = None  # lo chequeamos en runtime

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


# ── DB helpers (bootstrap de strategy_config + run) ─────────────────────────────
def _stable_params_json(cfg: EmaConfig) -> str:
    """
    Genera un JSON canónico de la configuración de la estrategia
    (solo parámetros propios de la estrategia; no incluye símbolo/TF).
    """
    as_dict = {
        "fast": int(cfg.ema_fast),
        "slow": int(cfg.ema_slow),
        "allow_shorts": bool(getattr(cfg, "allow_shorts", False)),
        "stop_pct": float(getattr(cfg, "stop_pct", 0.0)),
        "alloc_usdt": float(getattr(cfg, "alloc_usdt", 0.0)),
    }
    return json.dumps(as_dict, sort_keys=True, separators=(",", ":"))

def _md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()

def _connect_db(db_url: str):
    if psycopg is None:
        raise RuntimeError(
            "No se encontró psycopg. Agrega 'psycopg[binary]>=3.1' (o psycopg2-binary) a requirements.txt"
        )
    return psycopg.connect(db_url)

def _upsert_strategy_config(con, strategy_code: str, params_json: str) -> int:
    params_hash = _md5(f"{strategy_code}||{params_json}")
    with con.cursor() as cur:
        cur.execute(
            """
            INSERT INTO strategy_configs (strategy_code, params_json, params_hash)
            VALUES (%s, %s::jsonb, %s)
            ON CONFLICT (strategy_code, params_hash)
            DO UPDATE SET params_json = strategy_configs.params_json
            RETURNING cfg_id;
            """,
            (strategy_code, params_json, params_hash),
        )
        row = cur.fetchone()
        if not row:
            raise RuntimeError("upsert strategy_configs no devolvió cfg_id")
        return int(row[0])

def _insert_run(con, run_id: str, cfg: EmaConfig, cfg_id: int) -> None:
    with con.cursor() as cur:
        cur.execute(
            """
            INSERT INTO runs (
                run_id, cfg_id, bot_id, instance_id, environment, venue,
                symbol, base_tf, started_at, notes
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now(), %s)
            """,
            (
                run_id,
                cfg_id,
                cfg.bot_id,
                getattr(cfg, "instance_id", socket.gethostname()),
                getattr(cfg, "run_env", "sim"),
                getattr(cfg, "venue", "binance-sim"),
                cfg.symbol,
                cfg.tf_primary,
                "ema runner",
            ),
        )

def _finish_run(con, run_id: str) -> None:
    with con.cursor() as cur:
        cur.execute(
            "UPDATE runs SET finished_at = now() WHERE run_id = %s AND finished_at IS NULL",
            (run_id,),
        )

def _attach_runtime_defaults(cfg: EmaConfig) -> None:
    """
    Adjunta defaults si tu EmaConfig aún no define estos campos.
    No rompe si ya existen.
    """
    if not hasattr(cfg, "instance_id"):
        setattr(cfg, "instance_id", os.getenv("INSTANCE_ID", socket.gethostname()))

    # run_env: 'sim' | 'paper' | 'prod'
    if not hasattr(cfg, "run_env"):
        run_env = os.getenv("RUN_ENV", os.getenv("MODE", "sim")).lower()
        if run_env not in ("sim", "paper", "prod"):
            run_env = "sim"
        setattr(cfg, "run_env", run_env)

    if not hasattr(cfg, "venue"):
        base_url = os.getenv("BINANCE_BASE_URL", "")
        venue = os.getenv("VENUE")
        if not venue:
            venue = "binance-sim" if ("localhost" in base_url or "host.docker.internal" in base_url) else "binance-futures"
        setattr(cfg, "venue", venue)

    if not hasattr(cfg, "db_url"):
        setattr(cfg, "db_url", os.getenv("DB_URL"))

    if not hasattr(cfg, "db_enabled"):
        setattr(cfg, "db_enabled", bool(getattr(cfg, "db_url", None)))


# ── http app (healthz + /metrics + info) ────────────────────────────────────────
def build_http_app(bot_id: str, run_id: Optional[str]) -> FastAPI:
    app = FastAPI()

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "run_id": run_id}

    @app.get("/metrics")
    def metrics():
        # Import local para evitar problemas de alcance en distintos paths
        from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
        return Response(generate_latest(REG), media_type=CONTENT_TYPE_LATEST)  # type: ignore[arg-type]

    @app.get("/info")
    def info():
        return {"bot": bot_id, "strategy": "ema_cross", "run_id": run_id}

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
    _attach_runtime_defaults(cfg)

    # --- DB bootstrap ---
    con = None
    run_id: Optional[str] = None
    cfg_id: Optional[int] = None

    if getattr(cfg, "db_enabled", False) and getattr(cfg, "db_url", None):
        try:
            con = _connect_db(cfg.db_url)  # type: ignore[arg-type]
            con.autocommit = False
            strategy_code = "ema_cross_v1"
            params_json = _stable_params_json(cfg)
            cfg_id = _upsert_strategy_config(con, strategy_code, params_json)
            run_id = str(uuid.uuid4())
            _insert_run(con, run_id, cfg, cfg_id)
            con.commit()
            log.info(
                "DB listo: cfg_id=%s run_id=%s env=%s venue=%s",
                cfg_id, run_id, getattr(cfg, "run_env", "sim"), getattr(cfg, "venue", "binance-sim"),
            )
        except Exception as e:
            if con:
                try:
                    con.rollback()
                except Exception:
                    pass
            log.exception("Error inicializando DB: %s", e)
            # No abortamos el bot: seguimos sin persistencia
            con = None
            run_id = None
            cfg_id = None
    else:
        log.info(
            "Persistencia DB deshabilitada (DB_ENABLED=%s, DB_URL set=%s)",
            getattr(cfg, "db_enabled", False), bool(getattr(cfg, "db_url", None))
        )

    # HTTP server (FastAPI)
    app = build_http_app(cfg.bot_id, run_id)
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

    # Guardamos en el executor datos que usaremos para persistir órdenes/fills
    execu.run_id = run_id  # type: ignore[attr-defined]
    execu.cfg_id = cfg_id  # type: ignore[attr-defined]
    execu.db_conn = con    # type: ignore[attr-defined]

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
        getattr(cfg, "alloc_usdt", 0.0),
        getattr(cfg, "stop_pct", 0.0),
        getattr(cfg, "allow_shorts", False),
    )

    try:
        # Mantener procesos
        await asyncio.gather(http_task, poll_task)
    finally:
        # Apagado ordenado
        try:
            await redis.close()
        except Exception:
            pass
        if con and run_id:
            try:
                _finish_run(con, run_id)
                con.commit()
            except Exception:
                try:
                    con.rollback()
                except Exception:
                    pass
            try:
                con.close()
            except Exception:
                pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
