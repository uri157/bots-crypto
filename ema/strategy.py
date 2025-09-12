# ema/strategy.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass
class _EMA:
    period: int
    value: Optional[float] = None

    @property
    def alpha(self) -> float:
        # EMA smoothing factor
        return 2.0 / (self.period + 1.0)

    def update(self, price: float) -> float:
        if self.value is None:
            self.value = float(price)
        else:
            a = self.alpha
            self.value = a * float(price) + (1.0 - a) * self.value
        return self.value


class EmaCrossStrategy:
    """
    Estrategia simple de cruce de EMAs.
    - Señal LONG cuando EMA(fast) cruza por encima de EMA(slow).
    - Señal SHORT cuando EMA(fast) cruza por debajo de EMA(slow).
    - 'NONE' si no hay cruce en esta vela cerrada.
    """

    def __init__(self, symbol: str, fast: int, slow: int) -> None:
        if fast <= 0 or slow <= 0:
            raise ValueError("EMA periods must be positive")
        # Por convención fast < slow (no forzamos, pero se recomienda)
        self.symbol = symbol.upper()
        self.fast_p = int(fast)
        self.slow_p = int(slow)

        self._ema_fast = _EMA(self.fast_p)
        self._ema_slow = _EMA(self.slow_p)

        self._last_close: Optional[float] = None
        self._prev_diff: Optional[float] = None  # ema_fast - ema_slow de la vela anterior

    # ---------- actualización de estado ----------

    def update(self, close: float) -> None:
        """Actualiza ambas EMAs con el cierre de la vela."""
        self._last_close = float(close)
        f = self._ema_fast.update(close)
        s = self._ema_slow.update(close)
        # Inicializamos _prev_diff la primera vez que ambas EMAs existen
        if self._prev_diff is None and f is not None and s is not None:
            self._prev_diff = f - s

    # alias comunes que a veces usa el executor
    def on_candle_close(self, close: float) -> None:
        self.update(close)

    def update_candle(self, close: float, *_ignored) -> None:
        self.update(close)

    # ---------- consulta de señal ----------

    def generate_signal(self, close: float) -> str:
        """
        Devuelve "LONG" | "SHORT" | "NONE".
        Acepta el close de la vela, actualiza EMAs y detecta si hubo cruce
        respecto del diff de la vela anterior.
        """
        # Actualizamos EMAs con el cierre dado
        self.update(close)

        f, s = self.get_emas()
        if f is None or s is None or self._prev_diff is None:
            return "NONE"

        diff = f - s
        signal = "NONE"

        # Cruce alcista: antes <= 0, ahora > 0
        if self._prev_diff <= 0.0 and diff > 0.0:
            signal = "LONG"
        # Cruce bajista: antes >= 0, ahora < 0
        elif self._prev_diff >= 0.0 and diff < 0.0:
            signal = "SHORT"

        # Actualizamos prev_diff para la próxima vela
        self._prev_diff = diff
        return signal

    # ---------- getters auxiliares ----------

    def get_emas(self) -> Tuple[Optional[float], Optional[float]]:
        return (self._ema_fast.value, self._ema_slow.value)

    def get_last_close(self) -> Optional[float]:
        return self._last_close

    def ready(self) -> bool:
        """True si ambas EMAs ya fueron inicializadas."""
        f, s = self.get_emas()
        return f is not None and s is not None

    # opcional, por compat
    def get_current_atr(self) -> Optional[float]:
        return None  # no usamos ATR en EMA simple

    # reset opcional
    def reset(self) -> None:
        self._ema_fast = _EMA(self.fast_p)
        self._ema_slow = _EMA(self.slow_p)
        self._last_close = None
        self._prev_diff = None
