from __future__ import annotations
import os
import socket
from dataclasses import dataclass

def _env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on")

def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return default

def _first_symbol() -> str:
    s = os.getenv("SYMBOLS") or os.getenv("SYMBOL") or "BTCUSDT"
    return s.split(",")[0].strip().upper()

def _normalize_run_env(v: str | None) -> str:
    v = (v or "sim").strip().lower()
    if v in ("simulation", "sim", "backtest"):
        return "sim"
    if v in ("paper", "papertrading", "paper_trading"):
        return "paper"
    if v in ("prod", "production", "live", "real"):
        return "prod"
    return v

@dataclass
class EmaConfig:
    # instrumentos / datos
    symbol: str = _first_symbol()
    tf_primary: str = os.getenv("TF_PRIMARY", "1h")

    # parámetros de estrategia
    ema_fast: int = int(os.getenv("EMA_FAST", "12"))
    ema_slow: int = int(os.getenv("EMA_SLOW", "26"))
    # acepta EMA_ALLOW_SHORTS o ALLOW_SHORTS
    allow_shorts: bool = _env_bool("EMA_ALLOW_SHORTS", _env_bool("ALLOW_SHORTS", False))
    stop_pct: float = _env_float("STOP_PCT", 0.0)      # 0.01 = 1% (0 => sin stop)

    # sizing simple
    alloc_usdt: float = _env_float("ALLOC_USDT", 1000.0)  # notional fijo por trade

    # runtime
    redis_url: str = os.getenv("REDIS_URL", "redis://redis:6379/0")
    http_port: int = int(os.getenv("HTTP_PORT", "9001"))
    bot_id: str = os.getenv("BOT_ID", "ema")

    # --- persistencia en Postgres (novedad) ---
    db_enabled: bool = _env_bool("DB_ENABLED", False)
    db_url: str = os.getenv("DB_URL", "")
    run_env: str = _normalize_run_env(os.getenv("RUN_ENV") or os.getenv("MODE"))
    instance_id: str = os.getenv("INSTANCE_ID", socket.gethostname())
    venue: str = os.getenv("VENUE", "binance-sim")

    # exchange (usados por common.exchange)
    binance_api_key: str = os.getenv("BINANCE_API_KEY", "")
    binance_api_secret: str = os.getenv("BINANCE_API_SECRET", "")
    # acepta BINANCE_TESTNET o TESTNET
    testnet: bool = _env_bool("BINANCE_TESTNET", _env_bool("TESTNET", False))
    binance_base_url: str = os.getenv("BINANCE_BASE_URL", "")
    binance_ws_base: str = os.getenv("BINANCE_WS_BASE", "")
