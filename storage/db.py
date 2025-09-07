# storage/db.py
from __future__ import annotations

import os
import uuid
from datetime import datetime, date
from typing import Optional, Dict, Any

import duckdb

# -----------------------------------------------------------------------------
# Config & connection
# -----------------------------------------------------------------------------
DB_PATH = os.getenv("DB_PATH", "./data/trading.db")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

# Single process-wide connection (DuckDB is in-process; avoid many writers)
_conn = duckdb.connect(database=DB_PATH, read_only=False)
# Optional profiling (safe to ignore if unsupported)
try:
    _conn.execute("PRAGMA enable_profiling='json'")
except Exception:
    pass

# Optional Redis handle for metric mirrors (set via set_redis_client)
_redis = None


def set_redis_client(redis_client) -> None:
    """Optionally inject a Redis client to mirror some metrics."""
    global _redis
    _redis = redis_client


def conn() -> duckdb.DuckDBPyConnection:
    return _conn


# -----------------------------------------------------------------------------
# Schema
# -----------------------------------------------------------------------------
def init_db() -> None:
    """Create DuckDB tables and practical indexes if they don't exist."""
    _conn.execute(
        """
        CREATE TABLE IF NOT EXISTS orders (
            order_id   TEXT PRIMARY KEY,
            symbol     TEXT,
            side       TEXT,
            type       TEXT,
            qty        DOUBLE,
            price      DOUBLE,
            status     TEXT,
            time       TIMESTAMP,
            client_id  TEXT,
            strategy   TEXT
        );
        """
    )
    _conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fills (
            fill_id   TEXT PRIMARY KEY,
            order_id  TEXT REFERENCES orders(order_id),
            symbol    TEXT,
            qty       DOUBLE,
            price     DOUBLE,
            fee       DOUBLE,
            time      TIMESTAMP
        );
        """
    )
    _conn.execute(
        """
        CREATE TABLE IF NOT EXISTS positions (
            symbol     TEXT,
            side       TEXT,
            qty        DOUBLE,
            entry_price DOUBLE,
            entry_time TIMESTAMP,
            strategy   TEXT,
            PRIMARY KEY(symbol, side, strategy)
        );
        """
    )
    # Keep UUIDs as TEXT generated on the Python side (portable, no DuckDB extension needed)
    _conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ledger (
            ledger_id  TEXT PRIMARY KEY,
            symbol     TEXT,
            strategy   TEXT,
            timestamp  TIMESTAMP,
            type       TEXT,   -- 'TRADE' | 'FUNDING' | 'INFO'
            amount     DOUBLE,
            reason     TEXT
        );
        """
    )
    _conn.execute(
        """
        CREATE TABLE IF NOT EXISTS metrics (
            ts     TIMESTAMP,
            name   TEXT,
            value  DOUBLE
        );
        """
    )

    # Indexes
    _conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_symbol ON orders(symbol);")
    _conn.execute("CREATE INDEX IF NOT EXISTS idx_ledger_time   ON ledger(timestamp);")
    _conn.execute("CREATE INDEX IF NOT EXISTS idx_metrics_name  ON metrics(name);")


# -----------------------------------------------------------------------------
# Inserts / updates
# -----------------------------------------------------------------------------
def insert_order(order: Dict[str, Any], strategy: str) -> None:
    """Insert an order row into `orders`."""
    _conn.execute(
        "INSERT OR REPLACE INTO orders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            str(order.get("orderId", "")),
            str(order.get("symbol", "")),
            str(order.get("side", "")),
            str(order.get("type", "")),
            float(order.get("origQty", order.get("qty", 0.0) or 0.0)),
            float(order.get("price", 0.0) or 0.0),
            str(order.get("status", "")),
            datetime.utcnow(),
            str(order.get("clientOrderId", order.get("client_id", ""))),
            strategy,
        ],
    )


def insert_fill(order_id: str, fill: Dict[str, Any]) -> None:
    """Insert a fill (execution) row into `fills`."""
    fill_id = str(fill.get("id") or fill.get("tradeId") or uuid.uuid4())
    _conn.execute(
        "INSERT OR REPLACE INTO fills VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            fill_id,
            order_id,
            str(fill.get("symbol", "")),
            float(fill.get("qty", fill.get("executedQty", 0.0) or 0.0)),
            float(fill.get("price", 0.0) or 0.0),
            float(fill.get("commission", 0.0) or 0.0),
            datetime.utcnow(),
        ],
    )


def open_position(symbol: str, side: str, qty: float, entry_price: float, strategy: str) -> None:
    """Create/overwrite an open position row."""
    _conn.execute(
        """
        INSERT INTO positions(symbol, side, qty, entry_price, entry_time, strategy)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(symbol, side, strategy) DO UPDATE SET
            qty = excluded.qty,
            entry_price = excluded.entry_price,
            entry_time = excluded.entry_time
        """,
        [symbol, side, float(qty), float(entry_price), datetime.utcnow(), strategy],
    )


def close_position(symbol: str, strategy: str) -> None:
    """Remove an open position row."""
    _conn.execute("DELETE FROM positions WHERE symbol = ? AND strategy = ?", [symbol, strategy])


def insert_ledger(
    bot_id: str,
    symbol1: str,
    symbol2: Optional[str],
    pnl: float,
    funding: float,
    reason: str = "",
) -> None:
    """
    Insert P&L (TRADE) and optional FUNDING entries into `ledger`,
    then refresh today's aggregated PnL metric.
    """
    ts = datetime.utcnow()
    # PNL as TRADE
    _conn.execute(
        "INSERT INTO ledger(ledger_id, symbol, strategy, timestamp, type, amount, reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [str(uuid.uuid4()), symbol1, bot_id, ts, "TRADE", float(pnl), reason],
    )
    # FUNDING (optional)
    if funding and abs(funding) > 0.0:
        _conn.execute(
            "INSERT INTO ledger(ledger_id, symbol, strategy, timestamp, type, amount, reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [str(uuid.uuid4()), symbol1, bot_id, ts, "FUNDING", float(funding), reason],
        )

    _update_daily_pnl_metric()


# -----------------------------------------------------------------------------
# Metrics helpers
# -----------------------------------------------------------------------------
def _update_daily_pnl_metric() -> None:
    """Recompute and mirror the current-day PnL to metrics table and Redis (if configured)."""
    today: date = datetime.utcnow().date()
    # Sum of today's ledger (TRADE+FUNDING)
    cur = _conn.execute(
        "SELECT COALESCE(SUM(amount), 0.0) FROM ledger WHERE DATE(timestamp) = ?",
        [today],
    ).fetchone()
    pnl_day = float(cur[0] or 0.0)

    _conn.execute(
        "INSERT INTO metrics(ts, name, value) VALUES (?, ?, ?)",
        [datetime.utcnow(), "pnl_day_usdt", pnl_day],
    )

    # Mirror to Redis for fast consumption by risk daemon, if available
    if _redis is not None:
        try:
            _redis.set("metrics:pnl_day_usdt", str(pnl_day))
        except Exception:
            pass
