"""Pure indicator reference extracted from the verified quant implementation.

Original source SHA-256: d0405f9b663912f058b8ce87a5759b42044bd97cbf4a0ddefaf7b4815175ea41.
Only indicator arithmetic is retained. No account, runtime, or deployment data.
"""
from __future__ import annotations
from dataclasses import dataclass
from math import isfinite
from backend.trend import Bar, HOUR_MS

@dataclass(frozen=True)
class SuperTrendReading:
    open_time_ms: int
    atr: float
    adx: float | None
    trend: int
    flip: int
    entry_signal: int

class SuperTrendIndicators:
    """Process each completed, contiguous 1h bar exactly once, in order."""

    def __init__(self, period: int = 14, multiplier: float = 3.0, adx_min: float = 20.0):
        if (type(period) is not int or period < 2 or not isfinite(multiplier) or multiplier <= 0
                or not isfinite(adx_min) or adx_min < 0):
            raise ValueError("invalid indicator parameters")
        self.period, self.multiplier, self.adx_min = period, multiplier, adx_min
        self.last: SuperTrendReading | None = None
        self._ha: tuple[float, float, float, float] | None = None
        self._plus = self._minus = 0.0
        self._upper = self._lower = 0.0
        self._adx_weight = 1.0

    def update(self, bar: Bar) -> SuperTrendReading:
        if (not bar.is_closed or bar.open_time_ms % HOUR_MS
                or bar.close_time_ms + 1 != bar.open_time_ms + HOUR_MS):
            raise ValueError("completed whole-hour bar required")
        if self.last is not None and bar.open_time_ms != self.last.open_time_ms + HOUR_MS:
            raise ValueError("duplicate, out-of-order, or missing bar")
        o, h, l, c = map(float, (bar.open, bar.high, bar.low, bar.close))
        hc = (o + h + l + c) / 4
        ho = (o + c) / 2 if self._ha is None else (self._ha[0] + self._ha[3]) / 2
        hh, hl = max(h, ho, hc), min(l, ho, hc)
        alpha, beta = 1 / self.period, 1 - 1 / self.period
        if self.last is None:
            atr, trend, flip, adx = hh - hl, -1, 0, None
            self._upper, self._lower = hc + self.multiplier * atr, hc - self.multiplier * atr
        else:
            assert self._ha is not None
            _, ph, pl, pc = self._ha
            tr = max(hh - hl, abs(hh - pc), abs(hl - pc))
            atr = beta * self.last.atr + alpha * tr
            up, down = hh - ph, pl - hl
            self._plus = beta * self._plus + alpha * (up if up > down and up > 0 else 0)
            self._minus = beta * self._minus + alpha * (down if down > up and down > 0 else 0)
            # Calculate DI explicitly for numerical parity with the reference.
            pdi, mdi = (100 * self._plus / atr, 100 * self._minus / atr) if atr else (0, 0)
            dx = 100 * abs(pdi - mdi) / (pdi + mdi) if pdi + mdi else None
            adx = self.last.adx
            if adx is not None:
                self._adx_weight *= beta
            if dx is not None:
                adx = dx if adx is None else (self._adx_weight * adx + alpha * dx) / (self._adx_weight + alpha)
                self._adx_weight = 1.0
            trend = self.last.trend
            if (trend == 1 and hc < self._lower) or (trend == -1 and hc > self._upper):
                trend = -trend
            flip = trend if trend != self.last.trend else 0
            upper, lower = hc + self.multiplier * atr, hc - self.multiplier * atr
            if trend == 1:
                self._upper, self._lower = upper, max(self._lower, lower)
            else:
                self._upper, self._lower = min(self._upper, upper), lower
        signal = flip if adx is not None and adx > self.adx_min and atr > 0 else 0
        self._ha = ho, hh, hl, hc
        self.last = SuperTrendReading(bar.open_time_ms, atr, adx, trend, flip, signal)
        return self.last
