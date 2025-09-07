# storage/models.py
from __future__ import annotations

from datetime import datetime
from typing import List, Optional, Literal

from pydantic import BaseModel, Field


class Order(BaseModel):
    """Orden enviada/registrada en el exchange."""
    order_id: str = Field(..., description="Exchange order id")
    symbol: str
    side: Literal["BUY", "SELL"]
    type: str
    qty: float
    price: float
    status: str
    time: datetime
    client_id: Optional[str] = None
    strategy: str


class Fill(BaseModel):
    """Ejecución (fill) asociada a una orden."""
    fill_id: str
    order_id: str
    symbol: str
    qty: float
    price: float
    fee: float = 0.0
    time: datetime


class Position(BaseModel):
    """Posición abierta por símbolo/estrategia."""
    symbol: str
    side: Literal["LONG", "SHORT"]
    qty: float
    entry_price: float
    entry_time: datetime
    strategy: str


class LedgerEntry(BaseModel):
    """Movimiento contable: PnL de trade, funding u otros eventos."""
    ledger_id: Optional[str] = None
    timestamp: datetime
    type: Literal["TRADE", "FUNDING", "INFO"]
    amount: float
    symbol: str
    strategy: str
    reason: Optional[str] = None


class Status(BaseModel):
    """Estado expuesto por la API de control del bot."""
    bot_id: str
    mode: Literal["paper", "live"]
    paused: bool
    lock_held: bool
    positions: List[Position] = []
    daily_pnl: float = 0.0
    loss_streak: int = 0


class Message(BaseModel):
    """Respuesta mínima de endpoints de control."""
    message: str
