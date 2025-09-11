# common/exchange.py
from __future__ import annotations

import asyncio
import math
import os
import time
from typing import Any, Mapping, Optional, Protocol, List, Dict

from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception

# binance-connector (sincrónico). Lo envolvemos con asyncio.to_thread.
from binance.um_futures import UMFutures
from binance.error import ClientError, ServerError  # disponible en binance-connector


import logging
log = logging.getLogger(__name__)

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


# ---------- Implementación para Binance USDⓈ-M (UMFutures) ----------

def _is_transient(exc: Exception) -> bool:
    """Heurística de errores recuperables: 429/5xx o códigos -1013/-2010/-2011."""
    msg = repr(exc)
    if isinstance(exc, ServerError):
        return True
    if "429" in msg or "Too Many Requests" in msg:
        return True
    for code in ("-1013", "-2010", "-2011"):  # invalid qty / insufficient / unknown order
        if code in msg:
            return True
    return False


def _base_url_for(testnet: bool) -> str:
    # Permitir override por env para el simulador local
    url = os.getenv("BINANCE_BASE_URL")
    if url: 
        return url
    return "https://testnet.binancefuture.com" if testnet else "https://fapi.binance.com"


class Exchange(IExchange):
    """
    Adaptador asíncrono sobre binance-connector (UMFutures, sincrónico debajo).
    - Cachea exchangeInfo para tickSize/stepSize/contractType.
    - Fuerza hedge (dual-side) si BINANCE_DUAL_SIDE=true.
    """

    def __init__(self, api_key: str, api_secret: str, testnet: bool = False) -> None:
        base_url = _base_url_for(testnet)
        # Las claves pueden ir vacías para endpoints públicos
        self._client = UMFutures(key=api_key or None, secret=api_secret or None, base_url=base_url)
        self._symbol_info: Dict[str, Dict[str, Any]] = {}
        # Warmup en background
        asyncio.create_task(self._warmup())

    async def _call(self, fn, /, *args, **kwargs):
        """Ejecuta llamadas sincrónicas del SDK en un hilo para no bloquear."""
        return await asyncio.to_thread(fn, *args, **kwargs)

    async def _warmup(self) -> None:
        """Carga exchangeInfo y configura dual-side con reintentos suaves."""
        # 1) exchangeInfo con backoff
        delay = 0.5
        for attempt in range(1, 31):  # ~30 intentos, máx ~10s de espera entre intentos
            try:
                await self._load_exchange_info()
                break
            except Exception as e:
                log.warning("exchange._warmup: exchangeInfo fallo (intento %d): %s", attempt, e)
                await asyncio.sleep(delay)
                delay = min(delay * 1.5, 10.0)
        else:
            log.error("exchange._warmup: no se pudo cargar exchangeInfo tras varios intentos; sigo sin bloquear.")
            return  # no bloqueamos el arranque; el bot igual puede reintentar más tarde

        # 2) setear hedge/dual-side con backoff (no bloqueante)
        dual_env = os.getenv("BINANCE_DUAL_SIDE", "true").lower() in ("1", "true", "yes")
        delay = 0.5
        for attempt in range(1, 11):
            try:
                await self.set_position_mode(dual=dual_env)
                break
            except Exception as e:
                log.warning("exchange._warmup: set_position_mode fallo (intento %d): %s", attempt, e)
                await asyncio.sleep(delay)
                delay = min(delay * 1.5, 10.0)
        # si falla, no pasa nada: no bloqueamos

    async def close(self) -> None:
        """UMFutures no mantiene sockets aquí; nada que cerrar explícitamente."""
        return

    # ----- ExchangeInfo cache -----

    async def _load_exchange_info(self) -> None:
        """
        Carga USDⓈ-M exchangeInfo y construye un mapa:
        {symbol: {"tickSize": float, "stepSize": float, "contractType": str, "baseAsset": str, "quoteAsset": str}}
        """
        info = await self._call(self._client.exchange_info)
        symbols = info.get("symbols", []) if isinstance(info, dict) else []
        cache: Dict[str, Dict[str, Any]] = {}
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
            await self._call(self._client.change_position_mode, dualSidePosition=dual)
        except Exception as e:
            raise ExchangeError(f"Position mode set failed: {e}")

    async def set_margin_type(self, symbol: str, margin_type: str) -> None:
        """'ISOLATED' o 'CROSSED' (idempotente)."""
        try:
            await self._call(self._client.change_margin_type, symbol=symbol, marginType=margin_type.upper())
        except ClientError as e:
            # Si ya está en ese modo, Binance devuelve error; lo ignoramos.
            msg = str(e)
            if "No need to change margin type" in msg or "margin type cannot be changed" in msg.lower():
                return
            raise
        except Exception as e:
            raise ExchangeError(f"Change margin type failed: {e}")

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        try:
            await self._call(self._client.change_leverage, symbol=symbol, leverage=int(leverage))
        except Exception as e:
            raise ExchangeError(f"Change leverage failed: {e}")

    # ----- Símbolos trimestrales -----

    async def list_quarterly_contracts(self, base: str) -> list[str]:
        """
        Retorna [CURRENT_QUARTER, NEXT_QUARTER] para base-asset dado (quote USDT).
        Ej.: 'BTC' -> ['BTCUSDT', ...] filtrando por contractType trimestral.
        """
        base = base.upper()
        if not self._symbol_info:
            await self._load_exchange_info()

        out: List[str] = []
        for sym, meta in self._symbol_info.items():
            if meta.get("baseAsset") == base and meta.get("quoteAsset") == "USDT":
                ct = meta.get("contractType")
                if ct in ("CURRENT_QUARTER", "NEXT_QUARTER"):
                    out.append(sym)
        # CURRENT primero
        out_sorted = sorted(
            out,
            key=lambda s: (0 if self._symbol_info.get(s, {}).get("contractType") == "CURRENT_QUARTER" else 1, s),
        )
        return out_sorted

    # ----- Precios / funding / basis -----

    async def get_mark_price(self, symbol: str) -> float:
        try:
            data = await self._call(self._client.mark_price, symbol=symbol)
            return float(data["markPrice"])
        except Exception as e:
            raise ExchangeError(f"Mark price fetch failed for {symbol}: {e}")

    async def get_funding_rate(self, symbol: str) -> float:
        """Tasa de funding *actual* (último dato disponible), valor decimal (p.ej., 0.0005 = 0.05%)."""
        try:
            data = await self._call(self._client.premium_index, symbol=symbol)
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
            rows = await self._call(self._client.funding_rate, symbol=symbol, limit=limit)
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
        type_: Optional[str] = None,
        qty: Optional[float] = None,
        *,
        price: Optional[float] = None,
        stop_price: Optional[float] = None,
        client_id: Optional[str] = None,
        reduce_only: bool = False,
        # -------- alias back-compat --------
        order_type: Optional[str] = None,
        quantity: Optional[float] = None,
        origQty: Optional[float] = None,
        newClientOrderId: Optional[str] = None,
        timeInForce: Optional[str] = None,
        **kwargs: Any,
    ) -> Mapping[str, Any]:
        """
        Back-compat: acepta order_type/quantity/origQty/newClientOrderId/timeInForce y los
        traduce a los nombres que espera binance-connector (UMFutures.new_order).
        """
        # --- tipo ---
        t = (type_ or order_type or kwargs.get("type") or kwargs.get("orderType"))
        if not t:
            raise ExchangeError("place_order: missing order type")
        t = str(t).upper()

        # --- cantidad ---
        q = None
        for cand in (qty, quantity, origQty, kwargs.get("qty")):
            if cand is not None:
                q = float(cand)
                break
        if q is None:
            raise ExchangeError("place_order: missing quantity")

        # --- client id / TIF ---
        cid = client_id or newClientOrderId or kwargs.get("clientOrderId")
        tif = timeInForce or kwargs.get("time_in_force") or kwargs.get("timeInForce")

        # Permite alias para precios
        if price is None:
            price = kwargs.get("limit_price") or kwargs.get("limitPrice")
        if stop_price is None:
            stop_price = kwargs.get("stop_price") or kwargs.get("stopPrice")

        params: Dict[str, Any] = {
            "symbol": symbol,
            "side": str(side).upper(),
            "type": t,
            "quantity": q,
        }
        if cid:
            params["newClientOrderId"] = cid
        if price is not None:
            params["price"] = float(price)
        if stop_price is not None:
            params["stopPrice"] = float(stop_price)
        if reduce_only:
            params["reduceOnly"] = True
        if t == "LIMIT" and not tif:
            tif = "GTC"
        if tif:
            params["timeInForce"] = tif

        try:
            return await asyncio.to_thread(self._client.new_order, **params)
        except Exception as e:
            # tenacity decide si reintenta
            raise e

    @retry(
        reraise=True,
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
        retry=retry_if_exception(_is_transient),
    )
    async def cancel_open_orders(self, symbol: str) -> None:
        try:
            await self._call(self._client.cancel_all_open_orders, symbol=symbol)
        except Exception as e:
            # si ya no hay órdenes abiertas, Binance puede devolver error; se ignora
            if "-2011" in repr(e):  # unknown order
                return
            raise e

    async def cancel_order(self, symbol: str, order_id: int | str) -> None:
        """Cancela una orden específica por ID (idempotente)."""
        try:
            await self._call(self._client.cancel_order, symbol=symbol, orderId=order_id)
        except Exception as e:
            # Binance devuelve -2011 si ya no existe → lo ignoramos
            if "-2011" in repr(e):
                return
            raise ExchangeError(f"Cancel order failed: {e}")
