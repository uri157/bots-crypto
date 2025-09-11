from typing import Optional
from .orders import OrderService

class StopManager:
    def __init__(self, orders: OrderService):
        self.orders = orders

    async def place_initial(self, symbol: str, is_long: bool, qty: float, stop_price: float, client_id: str) -> Optional[str]:
        side = "SELL" if is_long else "BUY"
        resp = await self.orders.stop_market(symbol, side, qty, stop_price, client_id, reduce_only=True)
        return str(resp.get("orderId", "")) if isinstance(resp, dict) else None

    async def move(self, symbol: str, current_stop_id: Optional[str], is_long: bool, qty: float, new_price: float, client_id: str) -> Optional[str]:
        if current_stop_id:
            try:
                await self.orders.cancel(symbol, current_stop_id)
            except Exception:
                pass
        side = "SELL" if is_long else "BUY"
        resp = await self.orders.stop_market(symbol, side, qty, new_price, client_id, reduce_only=True)
        return str(resp.get("orderId", "")) if isinstance(resp, dict) else None
