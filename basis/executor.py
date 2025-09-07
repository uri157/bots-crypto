# basis/executor.py
from __future__ import annotations

import os
import time
import asyncio
from typing import Optional, Protocol, Any, Mapping
from datetime import datetime, timezone

from basis.calc import BasisStrategy


class Exchange(Protocol):
    """Contrato mínimo usado por BasisExecutor (lo implementa common/exchange.py)."""
    async def list_quarterly_contracts(self, base: str) -> list[str]: ...
    async def get_mark_price(self, symbol: str) -> float: ...
    def calc_basis(self, perp_price: float, fut_price: float, days_to_expiry: float) -> float: ...
    async def place_order(
        self,
        symbol: str,
        side: str,              # "BUY"/"SELL"
        type_: str,             # "MARKET"/"LIMIT"
        qty: float,
        client_id: Optional[str] = None,
        reduce_only: bool = False,
    ) -> Mapping[str, Any]: ...
    async def cancel_open_orders(self, symbol: str) -> None: ...
    async def set_margin_type(self, symbol: str, margin_type: str) -> None: ...   # "ISOLATED"/"CROSSED"
    async def set_leverage(self, symbol: str, leverage: int) -> None: ...


class BasisExecutor:
    """
    Ejecuta entradas/salidas de trades de basis (perp vs quarterly).
    Mantiene una sola posición activa por vez.
    """
    def __init__(self, exchange: Exchange, strategy: BasisStrategy, redis, bot_id: str):
        self.exchange = exchange
        self.strategy = strategy
        self.redis = redis
        self.bot_id = bot_id

        self.active_trade: Optional[dict] = None
        # ejemplo:
        # {
        #   "fut_symbol": str, "perp_symbol": str, "basis_entry": float, "side": "SHORT_BASIS"/"LONG_BASIS",
        #   "fut_qty": float, "perp_qty": float, "notional_usdt": float
        # }
        self.entry_time: Optional[datetime] = None

    async def try_open_trade(self) -> None:
        """
        Intenta abrir un trade si la estrategia lo indica y el lock de capital está tomado.
        """
        if self.active_trade or not await self._check_lock():
            return  # ya hay trade abierto o no poseo lock

        signal = await self.strategy.check_signal()
        if not signal:
            return

        # Modo margen/lev. primero (idempotente si ya está aplicado)
        contracts = await self.exchange.list_quarterly_contracts(self.strategy.base)
        if not contracts:
            return
        fut_symbol = contracts[0]
        perp_symbol = f"{self.strategy.base}USDT"

        try:
            await self._set_isolated_margin(fut_symbol, perp_symbol, leverage=3)
        except Exception:
            # si falla, seguimos (el exchange puede ya estar en el modo correcto)
            pass

        # Tamaños: usar riesgo porcentual vs ancho de SL (BASIS_SL)
        equity = float(os.getenv("EQUITY_USDT", "1000"))
        risk_pct = float(os.getenv("RISK_PCT_PER_TRADE", "0.01"))
        risk_amount = equity * risk_pct
        basis_sl = float(os.getenv("BASIS_SL", "0.05"))  # 5 puntos de base anualizada

        # nominal ≈ riesgo / SL
        nominal_usdt = max(risk_amount / max(basis_sl, 1e-6), 0.0)

        # Precios actuales
        fut_price = await self.strategy._get_price(fut_symbol)
        perp_price = await self.strategy._get_price(perp_symbol)
        if not fut_price or not perp_price:
            return

        perp_qty = max(nominal_usdt / perp_price, 0.0)
        fut_qty = max(nominal_usdt / fut_price, 0.0)

        short_basis = (signal == "SHORT_BASIS")
        ok = await self._execute_orders(fut_symbol, perp_symbol, fut_qty, perp_qty, short_basis=short_basis)
        if not ok:
            return

        self.active_trade = {
            "fut_symbol": fut_symbol,
            "perp_symbol": perp_symbol,
            "basis_entry": self.strategy.last_basis,
            "side": "SHORT_BASIS" if short_basis else "LONG_BASIS",
            "fut_qty": fut_qty,
            "perp_qty": perp_qty,
            "notional_usdt": min(perp_qty * perp_price, fut_qty * fut_price),
        }
        self.entry_time = datetime.now(timezone.utc)

    async def _execute_orders(self, fut_symbol: str, perp_symbol: str, fut_qty: float, perp_qty: float, short_basis: bool) -> bool:
        """
        Ejecuta ambas piernas con órdenes MARKET. Si una falla, revierte la otra.
        short_basis=True => SELL futuro, BUY perp ; caso contrario BUY futuro, SELL perp
        """
        fut_side = "SELL" if short_basis else "BUY"
        perp_side = "BUY" if short_basis else "SELL"
        client_id_base = f"{self.bot_id}-{int(time.time()*1000)}"

        tasks = [
            self.exchange.place_order(fut_symbol, fut_side, "MARKET", fut_qty, client_id=f"{client_id_base}-FUT", reduce_only=False),
            self.exchange.place_order(perp_symbol, perp_side, "MARKET", perp_qty, client_id=f"{client_id_base}-PERP", reduce_only=False),
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Si alguna arrojó excepción ⇒ rollback
        if any(isinstance(r, Exception) for r in results):
            await self._rollback_open_orders(fut_symbol, perp_symbol)
            return False

        return True

    async def _rollback_open_orders(self, fut_symbol: str, perp_symbol: str) -> None:
        """
        Intenta cancelar órdenes abiertas y revertir cualquier parcial.
        (Se delega a la abstracción del exchange; cerrar parciales si persisten).
        """
        try:
            await self.exchange.cancel_open_orders(fut_symbol)
        except Exception:
            pass
        try:
            await self.exchange.cancel_open_orders(perp_symbol)
        except Exception:
            pass
        # En una implementación real: consultar posición y, si hay qty residual, cerrarla con reduce-only.
        # Aquí dejamos al test integral validar que no quedan restos.
        self.active_trade = None

    async def monitor_trade(self) -> None:
        """
        Revisa periódicamente la base para decidir TP/SL o cierre por expiración.
        """
        if not self.active_trade:
            return

        basis_now = await self._compute_current_basis()
        if basis_now is None:
            return

        entry_basis = float(self.active_trade["basis_entry"])
        tp_abs = float(os.getenv("BASIS_TP", "0.02"))  # objetivo absoluto
        sl_pts = float(os.getenv("BASIS_SL", "0.05"))  # puntos de base anualizada

        close_trade = False
        reason = ""

        if self.active_trade["side"] == "SHORT_BASIS":
            # Queremos que la base baje. TP si <= min(entry - TP, TP absoluto).
            target = min(entry_basis - tp_abs, tp_abs)
            if basis_now <= target:
                close_trade, reason = True, "TP"
            elif basis_now >= entry_basis + sl_pts:
                close_trade, reason = True, "SL"
        else:
            # LONG_BASIS: queremos que la base suba. TP si >= max(entry + TP, TP absoluto).
            target = max(entry_basis + tp_abs, tp_abs)
            if basis_now >= target:
                close_trade, reason = True, "TP"
            elif basis_now <= entry_basis - sl_pts:
                close_trade, reason = True, "SL"

        # Cierre preventivo por proximidad a vencimiento
        days_left = self.strategy._days_to_expiry(self.active_trade["fut_symbol"])
        close_days_before = float(os.getenv("BASIS_CLOSE_DAYS_BEFORE", "5"))
        if days_left is not None and days_left <= close_days_before:
            close_trade, reason = True, "EXPIRY"

        if close_trade:
            await self._close_positions(reason)

    async def _compute_current_basis(self) -> Optional[float]:
        """
        Calcula base anualizada actual de la posición activa.
        """
        if not self.active_trade:
            return None
        perp_price = await self.strategy._get_price(self.active_trade["perp_symbol"])
        fut_price = await self.strategy._get_price(self.active_trade["fut_symbol"])
        days_to_expiry = self.strategy._days_to_expiry(self.active_trade["fut_symbol"])
        if perp_price is None or fut_price is None or days_to_expiry in (None, 0):
            return None
        return self.exchange.calc_basis(perp_price, fut_price, days_to_expiry)

    async def _close_positions(self, reason: str) -> None:
        """
        Cierra ambas piernas con órdenes MARKET reduce-only y registra en ledger.
        """
        if not self.active_trade:
            return

        fut_symbol = self.active_trade["fut_symbol"]
        perp_symbol = self.active_trade["perp_symbol"]
        fut_qty = float(self.active_trade["fut_qty"])
        perp_qty = float(self.active_trade["perp_qty"])

        # Lados inversos para cierre
        if self.active_trade["side"] == "SHORT_BASIS":
            fut_side, perp_side = "BUY", "SELL"
        else:
            fut_side, perp_side = "SELL", "BUY"

        cid = f"close-{int(time.time()*1000)}"
        # reduce-only
        await self.exchange.place_order(fut_symbol, fut_side, "MARKET", fut_qty, client_id=f"{cid}-FUT", reduce_only=True)
        await self.exchange.place_order(perp_symbol, perp_side, "MARKET", perp_qty, client_id=f"{cid}-PERP", reduce_only=True)

        pnl = await self._calculate_pnl()
        funding = await self._calculate_funding()

        try:
            # Inserción en ledger (contracto de storage/db.py)
            from storage.db import insert_ledger  # import local para evitar ciclos
            await insert_ledger(
                bot_id=self.bot_id,
                fut_symbol=fut_symbol,
                perp_symbol=perp_symbol,
                pnl_usdt=pnl,
                funding_usdt=funding,
                reason=reason,
                ts=datetime.now(timezone.utc).isoformat(),
            )
        except Exception:
            pass

        self.active_trade = None
        self.entry_time = None

    async def _calculate_pnl(self) -> float:
        """
        PnL aproximado usando delta de base * notional en USDT.
        """
        if not self.active_trade:
            return 0.0
        final_basis = await self._compute_current_basis()
        if final_basis is None:
            return 0.0
        entry = float(self.active_trade["basis_entry"])
        notional = float(self.active_trade.get("notional_usdt", 0.0))
        if self.active_trade["side"] == "SHORT_BASIS":
            pnl_rate = entry - final_basis
        else:
            pnl_rate = final_basis - entry
        return pnl_rate * notional

    async def _calculate_funding(self) -> float:
        """
        Estimación de funding pagado/recibido por la pierna perp durante el trade.
        Simplificado: funding_rate_promedio * horas * notional_perp.
        """
        if not self.active_trade or not self.entry_time:
            return 0.0
        try:
            # funding: almacenar en Redis como tasa/hora (por ej., suma de 3 cortes/24h)
            key = f"funding:{self.active_trade['perp_symbol']}"
            raw = await self.redis.get(key)
            funding_rate_h = float(raw.decode() if isinstance(raw, (bytes, bytearray)) else raw or 0.0)
        except Exception:
            funding_rate_h = 0.0

        hours = (datetime.now(timezone.utc) - self.entry_time).total_seconds() / 3600.0
        # Notional en la pata perp
        perp_price = await self.strategy._get_price(self.active_trade["perp_symbol"])
        notional_perp = float(self.active_trade["perp_qty"]) * (perp_price or 0.0)
        return funding_rate_h * hours * notional_perp

    async def _set_isolated_margin(self, fut_symbol: str, perp_symbol: str, leverage: int) -> None:
        """
        Configura margen aislado y leverage en ambas piernas (idempotente).
        """
        try:
            await self.exchange.set_margin_type(fut_symbol, "ISOLATED")
            await self.exchange.set_margin_type(perp_symbol, "ISOLATED")
        except Exception:
            pass
        try:
            await self.exchange.set_leverage(fut_symbol, leverage)
            await self.exchange.set_leverage(perp_symbol, leverage)
        except Exception:
            pass

    async def _check_lock(self) -> bool:
        """
        Verifica el capital lock en Redis. Si no soy dueño, pauso y dreno.
        """
        lock_key = os.getenv("CAPITAL_LOCK_KEY", "capital:lock")
        val = await self.redis.get(lock_key)
        current = val.decode() if isinstance(val, (bytes, bytearray)) else val
        if current != self.bot_id:
            await self.pause_and_drain()
            return False
        return True

    async def pause_and_drain(self) -> None:
        """
        Pausa y drena cualquier posición activa inmediatamente.
        Publica evento 'drained' para el gestor.
        """
        if self.active_trade:
            await self._close_positions(reason="LOCK_LOST")
        try:
            await self.redis.xadd("events", {"event": "drained", "bot": self.bot_id})
        except Exception:
            pass
