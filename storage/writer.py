# storage/writer.py
from __future__ import annotations
import os, time, asyncio
from typing import Optional, Dict, Any, Tuple

from storage.backlog import DiskBacklog

# Intentamos psycopg3; si no, psycopg2
try:
    import psycopg as psy  # psycopg 3
    _PSY3 = True
except Exception:
    import psycopg2 as psy # type: ignore
    _PSY3 = False


class DBWriter:
    """
    - enqueue(event): O(1), no bloquea trading (mete en asyncio.Queue).
    - Un consumidor asíncrono intenta escribir en Postgres.
    - Si falla, guarda en backlog JSONL y sigue.
    - Un lazo periódico drena el backlog (best effort con backoff).
    """
    def __init__(
        self,
        db_url: Optional[str],
        backlog_path: str = "/app/data/db_backlog.jsonl",
        drain_interval_s: float = 5.0,
    ) -> None:
        self.db_url = db_url
        self.backlog = DiskBacklog(backlog_path)
        self._q: asyncio.Queue[Dict[str, Any]] = asyncio.Queue(maxsize=10_000)
        self._drain_interval_s = float(drain_interval_s)
        self._consumer_task: Optional[asyncio.Task] = None
        self._backlog_task: Optional[asyncio.Task] = None
        self._con = None  # conexión psycopg/psycopg2
        self._stop = False

    async def start(self) -> None:
        if not self.db_url:
            # sin DB_URL → modo no-op (pero conservamos backlog si alguien lo usa explícito)
            return
        # conectar
        self._con = await self._connect()
        self._consumer_task = asyncio.create_task(self._consumer_loop())
        self._backlog_task = asyncio.create_task(self._backlog_loop())

    async def close(self) -> None:
        self._stop = True
        if self._consumer_task:
            self._consumer_task.cancel()
        if self._backlog_task:
            self._backlog_task.cancel()
        try:
            if self._con:
                if _PSY3:
                    await asyncio.to_thread(self._con.close)
                else:
                    self._con.close()
        except Exception:
            pass

    def enqueue(self, event: Dict[str, Any]) -> None:
        try:
            self._q.put_nowait(event)
        except Exception:
            # si la cola local está llena, como red de seguridad
            self.backlog.append(event)

    # --------- integración de alto nivel (helpers) ---------

    def emit_order(self, **kwargs: Any) -> None:
        """
        kwargs esperados:
          run_id, bot_id, instance_id, environment, venue, symbol,
          client_order_id, venue_order_id, side, order_type, status,
          time_in_force, reduce_only, price, stop_price, quantity,
          quote_qty, leverage, created_ts_ms, exchange_ts_ms
        """
        ev = {"type": "order"}
        ev.update({k: v for k, v in kwargs.items() if v is not None})
        self.enqueue(ev)

    def emit_fill(self, **kwargs: Any) -> None:
        """
        kwargs esperados:
          run_id, venue, client_order_id, seq, ts_ms, price, qty, quote_qty,
          fee, fee_asset, is_maker, realized_pnl, side
        """
        ev = {"type": "fill"}
        ev.update({k: v for k, v in kwargs.items() if v is not None})
        self.enqueue(ev)

    # --------- loops internos ---------

    async def _consumer_loop(self) -> None:
        backoff = 0.5
        while not self._stop:
            ev = await self._q.get()
            try:
                self._write_event(ev)
                backoff = 0.5  # reset backoff si hubo éxito
            except Exception:
                # a backlog y backoff
                self.backlog.append(ev)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 10.0)  # hasta 10s
            finally:
                self._q.task_done()

    async def _backlog_loop(self) -> None:
        while not self._stop:
            try:
                # drena hasta 500 eventos por pasada
                self.backlog.drain(self._write_event, max_batch=500)
            except Exception:
                pass
            await asyncio.sleep(self._drain_interval_s)

    # --------- acceso a DB ---------

    async def _connect(self):
        if _PSY3:
            # psycopg3: abrir en modo autocommit False
            return await asyncio.to_thread(psy.connect, self.db_url)
        else:
            return psy.connect(self.db_url)  # type: ignore

    def _commit(self):
        if self._con:
            self._con.commit()

    def _write_event(self, ev: Dict[str, Any]) -> None:
        t = ev.get("type")
        if t == "order":
            self._upsert_order(ev)
        elif t == "fill":
            self._insert_fill(ev)
        else:
            # desconocido → ignorar (o registrar)
            return

    # ---- SQL helpers ----

    def _upsert_order(self, ev: Dict[str, Any]) -> None:
        """
        Idempotencia por (venue, client_order_id).
        Retorna (implícitamente) el order_uid existente/nuevo para posteriores fills.
        """
        sql = """
        INSERT INTO orders (
          run_id, bot_id, instance_id, environment, venue, symbol,
          client_order_id, venue_order_id, side, order_type, status,
          time_in_force, reduce_only, price, stop_price, quantity,
          quote_qty, leverage, created_ts_ms, exchange_ts_ms
        ) VALUES (
          %(run_id)s, %(bot_id)s, %(instance_id)s, %(environment)s, %(venue)s, %(symbol)s,
          %(client_order_id)s, %(venue_order_id)s, %(side)s, %(order_type)s, %(status)s,
          %(time_in_force)s, %(reduce_only)s, %(price)s, %(stop_price)s, %(quantity)s,
          %(quote_qty)s, %(leverage)s, %(created_ts_ms)s, %(exchange_ts_ms)s
        )
        ON CONFLICT (venue, client_order_id)
        DO UPDATE SET
          status = EXCLUDED.status,
          venue_order_id = COALESCE(orders.venue_order_id, EXCLUDED.venue_order_id),
          exchange_ts_ms = COALESCE(EXCLUDED.exchange_ts_ms, orders.exchange_ts_ms)
        """
        with self._con.cursor() as cur:  # type: ignore
            cur.execute(sql, ev)
        self._commit()

    def _select_order_uid(self, venue: str, client_order_id: str) -> Optional[int]:
        sql = "SELECT order_uid FROM orders WHERE venue=%s AND client_order_id=%s"
        with self._con.cursor() as cur:  # type: ignore
            cur.execute(sql, (venue, client_order_id))
            row = cur.fetchone()
            return int(row[0]) if row else None

    def _insert_fill(self, ev: Dict[str, Any]) -> None:
        # Buscamos order_uid por (venue, client_order_id)
        order_uid = self._select_order_uid(ev["venue"], ev["client_order_id"])
        if order_uid is None:
            # Si todavía no existe la orden, lo mandamos a backlog de nuevo
            raise RuntimeError("order_uid not found for fill")

        payload = dict(ev)
        payload["order_uid"] = order_uid

        sql = """
        INSERT INTO fills (
          order_uid, run_id, seq, ts_ms, price, qty, quote_qty, fee, fee_asset,
          is_maker, realized_pnl, side
        ) VALUES (
          %(order_uid)s, %(run_id)s, %(seq)s, %(ts_ms)s, %(price)s, %(qty)s, %(quote_qty)s,
          %(fee)s, %(fee_asset)s, %(is_maker)s, %(realized_pnl)s, %(side)s
        )
        ON CONFLICT (order_uid, seq) DO NOTHING
        """
        with self._con.cursor() as cur:  # type: ignore
            cur.execute(sql, payload)
        self._commit()
