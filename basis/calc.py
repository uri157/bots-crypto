# basis/calc.py
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional, Protocol


class Exchange(Protocol):
    """Contrato mínimo que debe cumplir common/exchange.py para Basis."""
    async def list_quarterly_contracts(self, base: str) -> list[str]: ...
    async def get_mark_price(self, symbol: str) -> float: ...
    def calc_basis(self, perp_price: float, fut_price: float, days_to_expiry: float) -> float: ...


class BasisStrategy:
    """
    Estrategia de basis (perp vs quarterly) para un activo base (e.g. 'BTC').
    Señal:
      - 'SHORT_BASIS' si basis anualizada > BASIS_UPPER
      - 'LONG_BASIS'  si basis anualizada < BASIS_LOWER
      - None si no hay oportunidad o está muy cerca del vencimiento
    """
    def __init__(self, base_symbol: str, exchange: Exchange, price_cache=None):
        self.base = base_symbol.upper()
        self.exchange = exchange
        self.price_cache = price_cache  # opcional: aioredis/dict-like con get()
        self.last_basis: Optional[float] = None
        self.entry_basis: Optional[float] = None

    async def check_signal(self) -> Optional[str]:
        """
        Evalúa oportunidad de entrada basis.
        Retorna 'SHORT_BASIS' / 'LONG_BASIS' o None.
        """
        # 1) Símbolos
        contracts = await self.exchange.list_quarterly_contracts(self.base)
        if not contracts:
            return None
        fut_symbol = contracts[0]  # se asume ordenado: más cercano primero
        perp_symbol = f"{self.base}USDT"

        # 2) Precios (mark)
        fut_price = await self._get_price(fut_symbol)
        perp_price = await self._get_price(perp_symbol)
        if fut_price is None or perp_price is None or perp_price <= 0:
            return None

        # 3) Días a vencimiento y ventana de bloqueo
        dte = self._days_to_expiry(fut_symbol)
        if dte is None:
            return None
        close_days_before = float(os.getenv("BASIS_CLOSE_DAYS_BEFORE", "5"))
        if dte <= 0 or dte < close_days_before:
            return None  # demasiado cerca del vencimiento

        # 4) Basis anualizada
        basis = await self._calc_basis(perp_price, fut_price, dte)
        self.last_basis = basis

        upper = float(os.getenv("BASIS_UPPER", "0.10"))
        lower = float(os.getenv("BASIS_LOWER", "0.00"))

        if basis > upper:
            return "SHORT_BASIS"
        if basis < lower:
            return "LONG_BASIS"
        return None

    def _days_to_expiry(self, fut_symbol: str) -> Optional[float]:
        """
        Calcula días a expiración a partir del símbolo tipo 'BTCUSDT_250329'.
        """
        try:
            expiry_str = fut_symbol.split("_")[-1]  # 'YYMMDD'
            exp = datetime.strptime(expiry_str, "%y%m%d").replace(tzinfo=timezone.utc)
            now = datetime.now(tz=timezone.utc)
            return max((exp - now).total_seconds() / 86400.0, 0.0)
        except Exception:
            return None

    async def _get_price(self, symbol: str) -> Optional[float]:
        """
        Obtiene mark price del símbolo (perp o quarterly). Fallback opcional a caché.
        """
        try:
            return float(await self.exchange.get_mark_price(symbol))
        except Exception:
            if self.price_cache is not None:
                try:
                    val = await self.price_cache.get(f"price:{symbol}")
                    return float(val) if val is not None else None
                except Exception:
                    return None
            return None

    async def _calc_basis(self, perp_price: float, fut_price: float, days_to_expiry: float) -> float:
        """
        Basis anualizada: ((FUT/Perp) - 1) / (días/365).
        Usa helper del exchange si está disponible; si falla, calcula aquí.
        """
        try:
            return float(self.exchange.calc_basis(perp_price, fut_price, days_to_expiry))
        except Exception:
            # Fallback local
            return ((fut_price / perp_price) - 1.0) / (days_to_expiry / 365.0)
