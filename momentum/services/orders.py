from typing import Any, Mapping, Optional
from common.exchange import Exchange, ExchangeError

class OrderService:
    def __init__(self, exchange: Exchange):
        self.ex = exchange

    async def market(self, symbol: str, side: str, qty: float, client_id: str) -> Mapping[str,Any]:
        return await self.ex.place_order(symbol=symbol, side=side, order_type="MARKET", quantity=qty, client_id=client_id)

    async def stop_market(self, symbol: str, side: str, qty: float, stop_price: float, client_id: str, reduce_only: bool=True) -> Mapping[str,Any]:
        return await self.ex.place_order(symbol=symbol, side=side, order_type="STOP_MARKET",
                                         quantity=qty, stop_price=stop_price, client_id=client_id, reduce_only=reduce_only)

    async def cancel(self, symbol: str, order_id: int|str) -> None:
        cancel = getattr(self.ex, "cancel_order", None)
        if callable(cancel):
            await cancel(symbol, order_id=order_id)
            return
        await self.ex.cancel_open_orders(symbol)

    async def cancel_all(self, symbol: str) -> None:
        await self.ex.cancel_open_orders(symbol)
