# riskd/guard.py
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Dict, Optional

from aiohttp import ClientSession


class RiskGuardian:
    """
    Monitorea riesgos globales y controla bots vía HTTP (/pause, /resume).
    - Lee métricas de Redis (pnl, ws downtime, latencia).
    - Escucha eventos de los bots desde el Stream 'events' (XREAD).
    - Aplica circuit breakers globales y alerta por Telegram.
    """

    def __init__(self, redis, bots_api: Dict[str, str]) -> None:
        """
        :param redis: cliente aioredis
        :param bots_api: map bot -> base URL (p.ej., {"momentum": "http://momentum:9001"})
        """
        self.redis = redis
        self.bots_api = bots_api
        self.alerted = False
        # Estado del stream
        self._events_stream = os.getenv("EVENTS_STREAM", "events")
        # Posición desde la cual leer (iniciar desde '$' => solo nuevos eventos)
        self._last_event_id = os.getenv("EVENTS_START_ID", "$")

    # ---------- Bucle principal ----------

    async def monitor(self) -> None:
        """Lanza tareas de métricas y eventos."""
        await self._check_time_sync()  # chequeo inicial
        events_task = asyncio.create_task(self._events_loop())
        metrics_task = asyncio.create_task(self._metrics_loop())
        await asyncio.gather(events_task, metrics_task)

    # ---------- Métricas periódicas ----------

    async def _metrics_loop(self) -> None:
        interval = int(os.getenv("RISK_CHECK_INTERVAL_S", "30"))
        while True:
            try:
                await self.check_global_metrics()
            except Exception:
                # Evitar que una excepción termine el loop
                pass
            await asyncio.sleep(interval)

    async def check_global_metrics(self) -> None:
        """
        Revisa:
        - Drawdown diario (por ratio o por USDT).
        - Downtime de market data (último tick).
        - Latencia de órdenes (p95) si disponible.
        Dispara pausa global si se violan umbrales.
        """
        # --- Daily drawdown ---
        dd_limit_ratio = float(os.getenv("DAILY_MAX_DD", "-0.05"))  # -5% por defecto
        # Preferir ratio directo; si no existe, derivarlo de pnl_usdt/equity
        pnl_day_ratio = await self._get_float("metrics:pnl_day_ratio")
        if pnl_day_ratio is None:
            pnl_usdt = await self._get_float("metrics:pnl_day_usdt", default=0.0) or 0.0
            equity = float(os.getenv("EQUITY_USDT", "100.0"))
            pnl_day_ratio = pnl_usdt / equity if equity > 0 else 0.0

        if pnl_day_ratio <= dd_limit_ratio:
            await self.pause_all_bots(reason=f"daily drawdown {pnl_day_ratio*100:.1f}%")
            self.alerted = True
            return  # ya pausamos, no sigamos chequeando

        # --- WS downtime ---
        max_ws_downtime = float(os.getenv("WS_MAX_DOWNTIME_S", "60"))
        last_price_ts = await self._get_float("metrics:last_price_ts")
        if last_price_ts:
            downtime = time.time() - last_price_ts
            if downtime > max_ws_downtime:
                await self.pause_all_bots(reason=f"market data feed down ({downtime:.0f}s)")
                self.alerted = True
                return

        # --- Latencia de órdenes (p95) ---
        max_order_latency = float(os.getenv("ORDER_LATENCY_MAX_MS", "1000"))
        p95_latency = await self._get_float("metrics:order_latency_p95_ms")
        if p95_latency and p95_latency > max_order_latency:
            await self.pause_all_bots(reason=f"order latency p95 {p95_latency:.0f}ms > {max_order_latency}ms")
            self.alerted = True
            return

        # Si todo OK y había alerta previa, intentamos reanudar (opcional)
        if self.alerted and os.getenv("AUTO_RESUME_ON_RECOVERY", "false").lower() == "true":
            await self.resume_all_bots()
            self.alerted = False

    # ---------- Eventos (Stream Redis) ----------

    async def _events_loop(self) -> None:
        """
        Lee eventos del Stream 'events' con XREAD bloqueante.
        Los bots publican p.ej.: {"event":"drained","bot":BOT_ID}
        """
        block_ms = int(os.getenv("EVENTS_BLOCK_MS", "1000"))  # 1s
        stream = self._events_stream

        # Asegurar que el stream exista (XADD de marcador si vacío)
        try:
            exists = await self.redis.exists(stream)
            if not exists:
                await self.redis.xadd(stream, {"event": "init"})
        except Exception:
            pass

        while True:
            try:
                resp = await self.redis.xread(streams=[stream], count=10, latest_ids=[self._last_event_id], timeout=block_ms)
                if not resp:
                    continue
                # resp es lista de tuplas (stream, [(id, fields), ...])
                for _stream, entries in resp:
                    for entry_id, fields in entries:
                        self._last_event_id = entry_id
                        await self._handle_event(fields)
            except Exception:
                # No romper el loop por errores transitorios
                await asyncio.sleep(0.5)

    async def _handle_event(self, fields: Dict[bytes, bytes]) -> None:
        """Procesa un evento del stream."""
        try:
            # Campos vienen como bytes; intentar parsear `event` y `bot`
            event = fields.get(b"event", b"").decode() if isinstance(fields, dict) else ""
            bot = fields.get(b"bot", b"").decode() if isinstance(fields, dict) else ""
            payload_raw = fields.get(b"payload")
            payload = json.loads(payload_raw.decode()) if payload_raw else {}
        except Exception:
            return

        if event == "drained":
            # Un bot cerró todas las posiciones; aquí podríamos reasignar capital o notificar
            await self.send_telegram_alert(f"[riskd] Bot drained: {bot}")
        elif event == "pause":
            await self.send_telegram_alert(f"[riskd] Bot paused: {bot}")
        elif event == "resume":
            await self.send_telegram_alert(f"[riskd] Bot resumed: {bot}")
        # Extensible: handle más tipos de evento según necesites

    # ---------- Acciones sobre bots ----------

    async def pause_all_bots(self, reason: str) -> None:
        for bot, base in self.bots_api.items():
            try:
                async with ClientSession() as sess:
                    await sess.post(f"{base}/pause", timeout=3)
            except Exception:
                pass
        await self.send_telegram_alert(f"Pausing all trading: {reason}")

    async def resume_all_bots(self) -> None:
        for bot, base in self.bots_api.items():
            try:
                async with ClientSession() as sess:
                    await sess.post(f"{base}/resume", timeout=3)
            except Exception:
                pass
        await self.send_telegram_alert("Resuming trading after risk conditions cleared.")

    # ---------- Utilidades ----------

    async def _check_time_sync(self) -> None:
        """
        Compara reloj local vs server time de Binance Futures.
        Pausa si `skew_ms` > TIME_SYNC_SKEW_MS.
        """
        try:
            max_skew = int(os.getenv("TIME_SYNC_SKEW_MS", "250"))
            server_time_ms: Optional[float] = await self._get_float("exchange:server_time_ms")
            if server_time_ms is None:
                async with ClientSession() as sess:
                    # endpoint público
                    async with sess.get("https://fapi.binance.com/fapi/v1/time", timeout=3) as r:
                        data = await r.json()
                        server_time_ms = float(data["serverTime"])
                # cachear por un rato
                await self.redis.set("exchange:server_time_ms", str(server_time_ms), ex=5)

            local_ms = time.time() * 1000.0
            skew = abs(local_ms - float(server_time_ms))
            if skew > max_skew:
                await self.pause_all_bots(reason=f"time skew {skew:.0f}ms > {max_skew}ms")
                self.alerted = True
        except Exception:
            # No bloquear por errores de red
            pass

    async def send_telegram_alert(self, message: str) -> None:
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            return
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = {"chat_id": chat_id, "text": message}
        try:
            async with ClientSession() as session:
                await session.post(url, data=data, timeout=3)
        except Exception:
            pass

    async def _get_float(self, key: str, default: Optional[float] = None) -> Optional[float]:
        try:
            val = await self.redis.get(key)
            if val is None:
                return default
            if isinstance(val, (bytes, bytearray)):
                val = val.decode()
            return float(val)
        except Exception:
            return default
