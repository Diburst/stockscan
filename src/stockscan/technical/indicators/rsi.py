"""RSI(14) technical confirmation score.

Tag-aware interpretation:

  - mean_reversion strategies want LOW RSI (pullback is real, oversold).
    Score is positive when RSI < 50, negative when RSI > 50.

  - trend_following / breakout strategies want MIDDLE-HIGH RSI (50–80,
    momentum is real but not extended). Score peaks around RSI=80; very
    high RSI (≥80) is capped at +0.5 because it signals exhaustion risk.

  - Unknown tags → 0 (neutral). The composite skips indicators that abstain.

When called with `strategy=None` (watchlist), returns a direction-agnostic
bullish-bias score: high RSI is positive, low RSI is negative, linear in
between.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
from pydantic import Field

from stockscan.indicators import rsi as compute_rsi
from stockscan.strategies.base import Strategy
from stockscan.technical.indicators.base import (
    TechnicalIndicator,
    TechnicalIndicatorParams,
)


class RSITechParams(TechnicalIndicatorParams):
    period: int = Field(14, ge=2, le=50, description="RSI lookback")


class TechnicalRSI(TechnicalIndicator):
    name = "rsi"
    description = (
        "Relative Strength Index. Low RSI confirms mean-reversion pullbacks; "
        "high (but not extreme) RSI confirms trend-following entries."
    )
    params_model = RSITechParams

    # ------------------------------------------------------------------
    def values(self, bars: pd.DataFrame, as_of: date) -> dict[str, float] | None:
        if "close" not in bars.columns or len(bars) < self.params.period + 5:
            return None
        series = compute_rsi(bars["close"], self.params.period)
        last = series.iloc[-1]
        if pd.isna(last):
            return None
        return {"value": float(last)}

    # ------------------------------------------------------------------
    def score(
        self,
        values: dict[str, float],
        strategy: type[Strategy] | None,
    ) -> float:
        rsi_v = values["value"]

        # Direction-agnostic neutral mode (watchlist).
        if strategy is None:
            return self.clamp((rsi_v - 50) / 30)

        tags = set(strategy.tags)

        # Mean reversion: low RSI = oversold = entry-confirming.
        if "mean_reversion" in tags:
            return self.clamp((50 - rsi_v) / 30)

        # Trend-following / breakouts: high (but not extreme) RSI is bullish.
        if "trend_following" in tags or "breakout" in tags:
            if rsi_v >= 80:
                # Extended; partial credit but reversal risk caps the upside.
                return 0.5
            return self.clamp((rsi_v - 50) / 30)

        # No applicable tag — abstain.
        return 0.0
