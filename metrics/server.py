# metrics/server.py
from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import APIRouter, FastAPI, Response
from prometheus_client import (
    Counter,
    Gauge,
    Histogram,
    CONTENT_TYPE_LATEST,
    generate_latest,
    REGISTRY as _DEFAULT_REGISTRY,   # <- default registry global
)

# ========= Definición de métricas (sobre la DEFAULT REGISTRY) =========

# Estado del proceso/bot
bot_up = Gauge("bot_up", "Bot process running (1=up)", ["bot"])

# Latencia de órdenes (ms) — buckets razonables para CEX
order_latency_ms = Histogram(
    "order_latency_ms",
    "Latency of order executions in ms",
    buckets=(5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000),
)

# Uptime del WS (segundos)
ws_uptime_s = Gauge("ws_uptime_s", "WebSocket uptime in seconds")

# PnL diario y winrate 7d
pnl_day_usdt = Gauge("pnl_day_usdt", "Daily PnL in USDT")
winrate_7d = Gauge("winrate_7d", "Win rate over last 7 days (0-1)")

# Fees y slippage
fees_usdt = Counter("fees_usdt", "Total fees paid in USDT")
slippage_bps = Histogram(
    "slippage_bps",
    "Order slippage in basis points",
    buckets=(1, 2, 5, 10, 20, 50, 100, 200),
)

# Funding y basis en vivo
funding_now_bps = Gauge("funding_now_bps", "Current funding rate in bps", ["symbol"])
basis_now_pct   = Gauge("basis_now_pct", "Current basis percentage", ["symbol"])

# Margen libre / ratio de margen
margin_ratio = Gauge("margin_ratio", "Current free margin ratio")

# Funding realizado (sumatorio)
funding_realized_usdt = Counter("funding_realized_usdt", "Total funding realized in USDT")

# Órdenes rechazadas por filtros de riesgo
rejected_orders_count = Counter("rejected_orders_count", "Count of orders rejected due to risk filters")

# ========= Export que el runner espera =========
# Usamos la default registry de prometheus_client
REG   = _DEFAULT_REGISTRY     # <- para generate_latest(REG)
BOT_UP = bot_up               # <- alias con el nombre que importa el runner

def start_metrics_http(_port: int, _bot: str) -> None:
    """No levantamos servidor aparte; el runner expone /metrics."""
    return

# ========= Helpers para actualizar métricas desde otros módulos =========

def set_bot_up(bot_id: str, up: bool) -> None:
    bot_up.labels(bot=bot_id).set(1.0 if up else 0.0)

def observe_order_latency_ms(value_ms: float) -> None:
    order_latency_ms.observe(float(value_ms))

def set_ws_uptime(seconds: float) -> None:
    ws_uptime_s.set(float(seconds))

def set_pnl_day_usdt(value: float) -> None:
    pnl_day_usdt.set(float(value))

def set_winrate_7d(value_0_1: float) -> None:
    winrate_7d.set(float(value_0_1))

def add_fees_usdt(value: float) -> None:
    fees_usdt.inc(float(value))

def observe_slippage_bps(value_bps: float) -> None:
    slippage_bps.observe(float(value_bps))

def set_funding_bps(symbol: str, bps: float) -> None:
    funding_now_bps.labels(symbol=symbol.upper()).set(float(bps))

def set_basis_pct(symbol: str, pct: float) -> None:
    basis_now_pct.labels(symbol=symbol.upper()).set(float(pct))

def set_margin_ratio(value: float) -> None:
    margin_ratio.set(float(value))

def add_funding_realized_usdt(value: float) -> None:
    funding_realized_usdt.inc(float(value))

def inc_rejected_orders(n: int = 1) -> None:
    rejected_orders_count.inc(n)

# ========= Registro de endpoints (opcional) =========

def register_metrics(
    app: FastAPI,
    bot_id: Optional[str] = None,
    health_cb: Optional[Callable[[], dict[str, Any]]] = None,
) -> None:
    """
    Registra /metrics y /healthz en la app FastAPI.
    Si `bot_id` se provee, marca bot_up=1 al iniciar y 0 al apagar.
    `health_cb` permite devolver datos adicionales en /healthz.
    """
    router = APIRouter()

    @router.get("/metrics")
    async def metrics() -> Response:
        # usamos la default registry (REG)
        content = generate_latest(REG)
        return Response(content=content, media_type=CONTENT_TYPE_LATEST)

    @router.get("/healthz")
    async def health() -> dict[str, Any]:
        base = {"status": "OK"}
        if health_cb:
            try:
                base.update(health_cb() or {})
            except Exception:
                base.update({"health_cb": "error"})
        return base

    app.include_router(router)

    if bot_id:
        @app.on_event("startup")
        async def _on_start() -> None:
            set_bot_up(bot_id, True)

        @app.on_event("shutdown")
        async def _on_stop() -> None:
            set_bot_up(bot_id, False)
