# momentum/strategy.py
from __future__ import annotations

import os
from typing import Optional, Tuple

import numpy as np
import pandas as pd
from ta.trend import EMAIndicator, ADXIndicator
from ta.volatility import AverageTrueRange


class MomentumStrategy:
    """
    Estrategia Momentum con filtro por funding y requisitos de volatilidad (ATR).
    Calcula señales en el cierre de la vela principal (p.ej., 1h).
    """

    def __init__(
        self,
        symbol: str,
        equity: float,
        risk_pct: float,
        atr_min_pct: float,
        atr_period: int = 14,
        ema_fast: int = 50,
        ema_slow: int = 200,
        adx_min: float = 20.0,
    ) -> None:
        self.symbol = symbol.upper()
        self.equity = float(equity)          # capital USDT asignado
        self.risk_pct = float(risk_pct)      # fracción de capital a arriesgar por trade (0.005–0.01)
        self.atr_min_pct = float(atr_min_pct)
        self.atr_period = int(atr_period)
        self.ema_fast = int(ema_fast)
        self.ema_slow = int(ema_slow)
        self.adx_min = float(adx_min)

        # Buffers de datos
        self._close: list[float] = []
        self._high: list[float] = []
        self._low: list[float] = []

        # Últimos valores
        self.last_signal: Optional[str] = None
        self._last_atr: Optional[float] = None
        self._last_close: Optional[float] = None

        # Ventana máxima a retener (para evitar crecer sin límite)
        self._max_window = max(self.ema_slow, self.atr_period) + 10

    # ---- API esperada por el executor ----

    def get_current_atr(self) -> Optional[float]:
        return self._last_atr

    def get_last_close(self) -> Optional[float]:
        return self._last_close

    def update_equity(self, equity: float) -> None:
        self.equity = float(equity)

    # ---- Ingesta de vela cerrada ----

    def update_candle(self, close: float, high: float, low: float) -> None:
        """Llamar al cerrar la vela principal."""
        self._close.append(float(close))
        self._high.append(float(high))
        self._low.append(float(low))

        # Mantener buffers acotados
        if len(self._close) > self._max_window:
            self._close.pop(0)
            self._high.pop(0)
            self._low.pop(0)

        self._last_close = float(close)

    # ---- Señal de entrada y sizing ----

    def generate_signal(
        self,
        funding_rate: float = 0.0,
        funding_long_max: float = 0.0,
        funding_short_min: float = 0.0,
    ) -> Tuple[str, float, float]:
        """
        Devuelve (signal, position_size_qty, stop_price)

        Reglas:
        - LONG si EMA(50) > EMA(200), ADX >= adx_min, ATR% >= atr_min_pct y funding <= funding_long_max
        - SHORT si EMA(50) < EMA(200), ADX >= adx_min, ATR% >= atr_min_pct y funding >= funding_short_min
        - NONE en caso contrario
        """
        n = len(self._close)
        if n < max(self.ema_slow, self.atr_period) + 5:
            return ("NONE", 0.0, 0.0)

        close_s = pd.Series(self._close, dtype="float64")
        high_s = pd.Series(self._high, dtype="float64")
        low_s = pd.Series(self._low, dtype="float64")

        try:
            ema_fast_val = float(EMAIndicator(close_s, self.ema_fast).ema_indicator().iloc[-1])
            ema_slow_val = float(EMAIndicator(close_s, self.ema_slow).ema_indicator().iloc[-1])
            adx_val = float(ADXIndicator(high_s, low_s, close_s, window=14).adx().iloc[-1])
            atr_val = float(AverageTrueRange(high_s, low_s, close_s, window=self.atr_period)
                            .average_true_range().iloc[-1])
        except Exception:
            return ("NONE", 0.0, 0.0)

        if not np.isfinite(ema_fast_val) or not np.isfinite(ema_slow_val) or not np.isfinite(adx_val) or not np.isfinite(atr_val):
            return ("NONE", 0.0, 0.0)

        price = float(close_s.iloc[-1])
        if price <= 0:
            return ("NONE", 0.0, 0.0)

        atr_pct = atr_val / price
        self._last_atr = atr_val

        trend_up = ema_fast_val > ema_slow_val
        trend_down = ema_fast_val < ema_slow_val

        signal = "NONE"
        if adx_val >= self.adx_min and atr_pct >= self.atr_min_pct:
            # filtros de funding (por hora)
            if trend_up and funding_rate <= float(funding_long_max):
                signal = "LONG"
            elif trend_down and funding_rate >= float(funding_short_min):
                signal = "SHORT"

        if signal == "NONE":
            self.last_signal = "NONE"
            return ("NONE", 0.0, 0.0)

        # Sizing por riesgo: risk_amount / stop_distance
        atr_stop_mult = float(os.getenv("ATR_STOP_MULT", "1.8"))
        stop_distance = atr_stop_mult * atr_val
        if stop_distance <= 0:
            self.last_signal = "NONE"
            return ("NONE", 0.0, 0.0)

        risk_amount = max(0.0, self.equity * self.risk_pct)
        qty = risk_amount / stop_distance  # cantidad en "contratos" (USDT / precio la ajustará el executor)
        if qty <= 0:
            self.last_signal = "NONE"
            return ("NONE", 0.0, 0.0)

        stop_price = price - stop_distance if signal == "LONG" else price + stop_distance
        self.last_signal = signal
        return (signal, float(qty), float(stop_price))
