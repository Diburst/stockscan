"""MACD(12, 26, 9) technical confirmation score.

Standard MACD:
  - macd line   = EMA(close, 12) − EMA(close, 26)
  - signal line = EMA(macd, 9)
  - histogram   = macd − signal

The histogram captures both direction (sign) and acceleration (slope),
which gives us a richer signal than RSI alone.

Tag-aware interpretation:

  - mean_reversion strategies want histogram NEGATIVE BUT RISING (the
    selloff is bottoming and reversing — confirms a pullback that's about
    to bounce). Histogram steady-negative or falling = contradicting.

  - trend_following / breakout strategies want histogram POSITIVE AND
    RISING (momentum is up and accelerating). Negative histogram or
    falling histogram = contradicting.

  - Unknown tags → 0 (neutral).

When called with `strategy=None` (watchlist), returns direction-agnostic
score: positive histogram + positive slope is bullish bias.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
from pydantic import Field

from stockscan.indicators import macd as compute_macd
from stockscan.strategies.base import Strategy
from stockscan.technical.indicators.base import (
    TechnicalIndicator,
    TechnicalIndicatorParams,
)


class MACDTechParams(TechnicalIndicatorParams):
    fast: int = Field(12, ge=2, le=50)
    slow: int = Field(26, ge=5, le=100)
    signal: int = Field(9, ge=2, le=30)


class TechnicalMACD(TechnicalIndicator):
    name = "macd"
    description = (
        "MACD histogram and slope. Mean-reversion confirms on negative "
        "histogram that's turning up; trend-following confirms on positive "
        "histogram that's accelerating."
    )
    params_model = MACDTechParams

    def values(self, bars: pd.DataFrame, as_of: date) -> dict[str, float] | None:
        # Need at least slow + signal periods + a bit of warmup for two
        # consecutive readings of the histogram.
        min_len = self.params.slow + self.params.signal + 5
        if "close" not in bars.columns or len(bars) < min_len:
            return None
        result = compute_macd(
            bars["close"], self.params.fast, self.params.slow, self.params.signal
        )
        hist = result["histogram"]
        if pd.isna(hist.iloc[-1]) or pd.isna(hist.iloc[-2]):
            return None
        return {
            "macd": float(result["macd"].iloc[-1]),
            "signal": float(result["signal"].iloc[-1]),
            "histogram": float(hist.iloc[-1]),
            "histogram_prev": float(hist.iloc[-2]),
        }

    def score(
        self,
        values: dict[str, float],
        strategy: type[Strategy] | None,
    ) -> float:
        hist = values["histogram"]
        hist_prev = values["histogram_prev"]
        slope = hist - hist_prev

        # Direction-agnostic neutral mode (watchlist).
        if strategy is None:
            sign = 1.0 if hist > 0 else -1.0
            slope_factor = 0.5 if slope > 0 else -0.5
            return self.clamp(0.7 * sign + 0.3 * slope_factor)

        tags = set(strategy.tags)

        # Mean reversion: want histogram negative but turning up (bottoming).
        if "mean_reversion" in tags:
            if hist < 0 and slope > 0:
                # Stronger reversal off the lows = stronger confirmation.
                # Slope magnitude relative to histogram size is the signal.
                strength = min(1.0, abs(slope) / max(abs(hist), 0.01))
                return self.clamp(0.4 + 0.6 * strength)
            if hist < 0 and slope <= 0:
                return -0.4  # still falling, contradicts
            # Histogram already positive — late to the party. Mild positive.
            return 0.1

        # Trend-following / breakouts: want positive histogram + rising.
        if "trend_following" in tags or "breakout" in tags:
            sign_score = 1.0 if hist > 0 else -1.0
            slope_score = 1.0 if slope > 0 else -0.4
            return self.clamp(0.6 * sign_score + 0.4 * slope_score)

        # No applicable tag — abstain.
        return 0.0
