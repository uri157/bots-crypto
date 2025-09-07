# common/exchange.py
from __future__ import annotations

import asyncio
import math
import os
from typing import Any, Mapping, Optional, Protocol

from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception

from binance.connector import AsyncClient
try:
    # binance-connector >= 3.x
    from binance.error import ClientError, ServerError  # type: ignore
except Exception:
    # Fallback por compatibilidad
    class ClientError(Exception): ...
    class ServerError(Exception): ...

# ---------- Excepciones ----------

class ExchangeError(Exception):
    """Errores fatales (no recuperables)."""


# ---------- Contratos (para linters y tests) ----------

class IExchange(Protocol):
    async def list_quarterly_contracts(self, base: str) -> list[str]: ...
    async def get_mark_price(self, symbol: str) -> float: ...
    def calc_basis(self, perp_price: float, fut_price: float, days_to_expiry: float) -> float: ...
    async def place_order(
        self,
        symbol: str,
        side: str,           # "BUY"/"SELL"
        type_: str,          # "MARKET"/"LIMIT"/"STOP_MARKET"
        qty: float,
        price: Optional[float] = None,
        stop_price: Optional[float] = None,
        client_id: Optional[str] = None,
        reduce_only: bool = False,
    ) -> Mapping[str, Any]: ...
    async def cancel_open_orders(self, symbol: str) -> None: ...
    async def set_margin_type(self, symbol: str, margin_type: str) -> None: ...
    async def set_leverage(self, symbol: str, leverage: int) -> None: ...
    async def set_position_mode(self, dual: bool) -> None: ...
    async def get_funding_rate(self, symbol: str) -> float: ...
    async def get_historical_funding(self, symbol: str, days: int) -> list[float]: ...
    async def get_tick_size(self, symbol: str) -> float: ...
    async def get_step_size(self, symbol: str) -> float: ...
    def round_price(self, symbol: str, price: float) -> float: ...
    def round_quantity(self, symbol: str, qty: float) -> float: ...


# ---------- Implementación para Binance USDⓈ-M ----------

def _is_transient(exc: Exception) -> bool:
    """Heurística de errores recuperables: 429/5xx o códigos -1013/-2010."""
    # tenacity usa esta función para decidir reintentos
    msg = repr(exc)
    if isinstance(exc, (ServerError,)):
        return True
    if "429" in msg or "Too Many Requests" in msg:
        return True
    # Códigos de Binance típicos de condiciones transitorias/orden inválida momentánea
    for code in ("-1013", "-2010", "-2011"):  # invalid qty/insufficient/unknown order
        if code in msg:
            return True
    return False


class Exchange(IExchange):
    """
    Adaptador asíncrono de Binance USDⓈ-M (perps + delivery trimestrales).
    - Cachea exchangeInfo para tickSize/stepSize y contractType.
    - Fuerza hedge (dual-side) si POSITION_MODE/ENV lo requiere.
    """

    def __init__(self, api_key: str, api_secret: str, testnet: bool = False) -> None:
        self.client = AsyncClient(api_key, api_secret, testnet=testnet)
        self._symbol_info: dict[str, dict[str, Any]] = {}
        # Warmups en background: exchangeInfo y hedge-mode si BINANCE_DUAL_SIDE=true
        asyncio.create_task(self._warmup())

    async def _warmup(self) -> None:
        await self._load_exchange_info()
        # Activa hedge dual-side si está seteado
        dual_env = os.getenv("BINANCE_DUAL_SIDE", "true").lower() in ("1", "true", "yes")
        try:
            await self.set_position_mode(dual=dual_env)
        except Exception:
            # No queremos bloquear por esto al inicio
            pass

    async def close(self) -> None:
        try:
            await self.client.close_connection()  # cierra sesiones http/ws
        except Exception:
            pass

    # ----- ExchangeInfo cache -----

    async def _load_exchange_info(self) -> None:
        """
        Carga USDⓈ-M futures exchangeInfo y construye un mapa:
        {symbol: {"tickSize": float, "stepSize": float, "contractType": str, "baseAsset": str, "quoteAsset": str}}
        """
        info = await self.client.futures_exchange_info()
        symbols = info.get("symbols", [])
        cache: dict[str, dict[str, Any]] = {}
        for s in symbols:
            filters = {f["filterType"]: f for f in s.get("filters", [])}
            tick = float(filters.get("PRICE_FILTER", {}).get("tickSize", "0.0"))
            step = float(filters.get("LOT_SIZE", {}).get("stepSize", "0.0"))
            cache[s["symbol"]] = {
                "tickSize": tick,
                "stepSize": step,
                "contractType": s.get("contractType"),  # PERPETUAL / CURRENT_QUARTER / NEXT_QUARTER
                "baseAsset": s.get("baseAsset"),
                "quoteAsset": s.get("quoteAsset"),
            }
        self._symbol_info = cache

    async def get_tick_size(self, symbol: str) -> float:
        if symbol not in self._symbol_info:
            await self._load_exchange_info()
        return float(self._symbol_info[symbol]["tickSize"])

    async def get_step_size(self, symbol: str) -> float:
        if symbol not in self._symbol_info:
            await self._load_exchange_info()
        return float(self._symbol_info[symbol]["stepSize"])

    def round_price(self, symbol: str, price: float) -> float:
        tick = float(self._symbol_info.get(symbol, {}).get("tickSize", 0.0))
        if tick <= 0:
            return float(price)
        # floor al múltiplo de tick
        return math.floor(price / tick) * tick

    def round_quantity(self, symbol: str, qty: float) -> float:
        step = float(self._symbol_info.get(symbol, {}).get("stepSize", 0.0))
        if step <= 0:
            return float(qty)
        return math.floor(qty / step) * step

    # ----- Modo de posición / margen / apalancamiento -----

    async def set_position_mode(self, dual: bool) -> None:
        """Configura hedge mode (dualSidePosition=True) en USDⓈ-M."""
        try:
            await self.client.futures_change_position_mode(dualSidePosition=dual)
        except Exception as e:
            raise ExchangeError(f"Position mode set failed: {e}")

    async def set_margin_type(self, symbol: str, margin_type: str) -> None:
        """'ISOLATED' o 'CROSSED' (idempotente)."""
        try:
            await self.client.futures_change_margin_type(symbol=symbol, marginType=margin_type.upper())
        except ClientError as e:
            # Si ya está en ese modo, Binance devuelve error; lo ignoramos.
            if "No need to change margin type" in str(e):
                return
            raise
        except Exception as e:
            raise ExchangeError(f"Change margin type failed: {e}")

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        try:
            await self.client.futures_change_leverage(symbol=symbol, leverage=int(leverage))
        except Exception as e:
            raise ExchangeError(f"Change leverage failed: {e}")

    # ----- Símbolos trimestrales -----

    async def list_quarterly_contracts(self, base: str) -> list[str]:
        """
        Retorna [CURRENT_QUARTER, NEXT_QUARTER] para base-asset dado (quote USDT).
        Ej.: 'BTC' -> ['BTCUSDT_250329', 'BTCUSDT_250627'] si existen.
        """
        base = base.upper()
        if not self._symbol_info:
            await self._load_exchange_info()

        # Filtra por base/quote y contractType != PERPETUAL
        out = []
        for sym, meta in self._symbol_info.items():
            if meta.get("baseAsset") == base and meta.get("quoteAsset") == "USDT":
                ct = meta.get("contractType")
                if ct in ("CURRENT_QUARTER", "NEXT_QUARTER"):
                    # Binance no siempre agrega el sufijo "_YYMMDD" en 'symbol'
                    # Para consistencia con el resto del código, intentamos obtener el símbolo “con fecha” desde mark-price
                    try:
                        mp = await self.client.futures_mark_price(symbol=sym)
                        # markPair puede traer 'pair' o 'time' pero no la fecha; dejamos el símbolo básico
                        out.append(sym)
                    except Exception:
                        out.append(sym)
        # Orden simple: CURRENT primero si está presente
        out_sorted = sorted(out, key=lambda s: (0 if "PERPETUAL" not in s and "CURRENT" in str(self._symbol_info.get(s, {}).get("contractType", "")) else 1, s))
        return out_sorted

    # ----- Precios / funding / basis -----

    async def get_mark_price(self, symbol: str) -> float:
        try:
            data = await self.client.futures_mark_price(symbol=symbol)
            return float(data["markPrice"])
        except Exception as e:
            raise ExchangeError(f"Mark price fetch failed for {symbol}: {e}")

    async def get_funding_rate(self, symbol: str) -> float:
        """Tasa de funding *actual* (por periodo de 8h), valor decimal (p.ej., 0.0005 = 0.05%)."""
        try:
            data = await self.client.futures_premium_index(symbol=symbol)
            # algunos SDK devuelven 'lastFundingRate' como string
            return float(data.get("lastFundingRate", 0.0))
        except Exception as e:
            raise ExchangeError(f"Funding rate fetch failed: {e}")

    async def get_historical_funding(self, symbol: str, days: int) -> list[float]:
        """
        Historial de funding de ~days (8 cortes por día).
        Devuelve lista de float (decimales, no %).
        """
        limit = max(8 * days, 8)  # 8 por día
        try:
            rows = await self.client.futures_funding_rate(symbol=symbol, limit=limit)
            return [float(r["fundingRate"]) for r in rows]
        except Exception as e:
            raise ExchangeError(f"Funding history fetch failed: {e}")

    def calc_basis(self, perp_price: float, fut_price: float, days_to_expiry: float) -> float:
        """
        Basis anualizada (decimal): ((FUT/Perp) - 1) / (days/365).
        Ej.: 0.12 => 12% anual.
        """
        days = max(float(days_to_expiry), 1e-9)
        return ((float(fut_price) / float(perp_price)) - 1.0) / (days / 365.0)

    # ----- Órdenes -----

    @retry(
        reraise=True,
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
        retry=retry_if_exception(_is_transient),
    )
    async def place_order(
        self,
        symbol: str,
        side: str,
        type_: str,
        qty: float,
        price: Optional[float] = None,
        stop_price: Optional[float] = None,
        client_id: Optional[str] = None,
        reduce_only: bool = False,
    ) -> Mapping[str, Any]:
        """
        Envía una orden a USDⓈ-M. Usa `newClientOrderId` para idempotencia.
        Respeta `reduce_only` para cierres.
        """
        params: dict[str, Any] = {
            "symbol": symbol,
            "side": side.upper(),
            "type": type_.upper(),
            "quantity": qty,
        }
        if client_id:
            params["newClientOrderId"] = client_id
        if price is not None:
            params["price"] = price
        if stop_price is not None:
            params["stopPrice"] = stop_price
        if reduce_only:
            params["reduceOnly"] = True

        # Slippage cap (opcional, desde ENV)
        slip_cap = float(os.getenv("SLIPPAGE_CAP_PCT", "0.002"))  # 0.2%
        try:
            last = await self.get_mark_price(symbol)
        except Exception:
            last = None
        if last and type_.upper() == "MARKET" and price is None:
            # Podrías validar post-fill; aquí hacemos una validación pre (no bloquea)
            pass

        try:
            res = await self.client.futures_create_order(**params)
            # Validación ligera de slippage post-fill si vino price avg
            if slip_cap and "avgPrice" in res and last:
                avg = float(res.get("avgPrice") or 0.0)
                if avg > 0 and abs(avg - last) / last > slip_cap:
                    # No revertimos aquí (orden ejecutada); delegamos a RiskD si corresponde.
                    pass
            return res
        except Exception as e:
            # tenacity decidirá si reintenta
            raise e

    @retry(
        reraise=True,
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
        retry=retry_if_exception(_is_transient),
    )
    async def cancel_open_orders(self, symbol: str) -> None:
        try:
            await self.client.futures_cancel_all_open_orders(symbol=symbol)
        except Exception as e:
            # si ya no hay órdenes abiertas, Binance puede devolver error; se ignora
            if "-2011" in repr(e):  # unknown order
                return
            raise e
