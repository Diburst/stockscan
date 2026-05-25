"""Trend stack & slope — reinforce-only context for the reversal score (spec §4.1).

Absolute intermediate trend: where price sits vs its 50-day (primary) and 200-day
(lighter context) SMAs, plus the 50-day slope. Bands are calibrated for
single-stock dispersion, NOT the index regime values (porting those is a bug —
they saturate every name; see the spec calibration note).

In the v2 composite this is a **reinforce-only** directional input (spec §6): it
only ever *adds* conviction when its sign agrees with the core reversal
direction; when it would oppose, the composite drops it (never vetoes a
counter-trend bottom or an exit). `score()` therefore just returns the natural
signed `raw`; the reinforce-only logic lives in `compute_technical_score`.

Pure math is in `_trend_values` (bars-only, no DB, no look-ahead).
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

import pandas as pd
from pydantic import Field

from stockscan.technical.indicators.base import (
    TechnicalIndicator,
    TechnicalIndicatorParams,
)

if TYPE_CHECKING:
    from stockscan.strategies.base import Strategy


def _clip(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _trend_values(
    close: pd.Series,
    *,
    pos50_band: float,
    pos200_band: float,
    slope_band: float,
    slope_window: int,
) -> dict[str, float] | None:
    """Signed trend read in [-1,+1] from a close series. `None` if < 60 bars.

    Graceful degradation: with 60–219 bars the 200-day term is dropped and its
    weight renormalized onto pos50/slope (so new-ish listings still score).
    """
    c = close.dropna()
    if len(c) < 60:
        return None
    last = float(c.iloc[-1])

    sma50 = c.rolling(50).mean()
    sma50_last = sma50.iloc[-1]
    if pd.isna(sma50_last) or float(sma50_last) <= 0:
        return None
    sma50_last = float(sma50_last)
    pos50 = _clip((last - sma50_last) / sma50_last / pos50_band)

    slope_n = 0.0
    if len(sma50) > slope_window:
        prev = sma50.iloc[-1 - slope_window]
        if pd.notna(prev) and float(prev) > 0:
            slope_n = _clip(((sma50_last - float(prev)) / float(prev)) / slope_band)

    out: dict[str, float] = {"sma50": round(sma50_last, 4), "pos50": pos50, "slope_n": slope_n}

    if len(c) >= 220:
        sma200_last = c.rolling(200).mean().iloc[-1]
        if pd.notna(sma200_last) and float(sma200_last) > 0:
            pos200 = _clip((last - float(sma200_last)) / float(sma200_last) / pos200_band)
            out["pos200"] = pos200
            out["raw"] = _clip(0.40 * pos50 + 0.20 * pos200 + 0.40 * slope_n)
            return out

    # pos200 unavailable → renormalize 0.40/0.40 onto pos50/slope (→ 0.5/0.5).
    out["raw"] = _clip(0.5 * pos50 + 0.5 * slope_n)
    return out


class TrendLocationParams(TechnicalIndicatorParams):
    pos50_band: float = Field(0.10, gt=0, description="±band on distance from the 50-day SMA.")
    pos200_band: float = Field(0.25, gt=0, description="±band on distance from the 200-day SMA.")
    slope_band: float = Field(0.05, gt=0, description="±band on 50-day SMA slope per window.")
    slope_window: int = Field(20, ge=5, le=60, description="Bars for the 50-day SMA slope.")


class TechnicalTrendLocation(TechnicalIndicator):
    name = "trend_location"
    description = (
        "Intermediate (50-day) trend + slope, with a lighter 200-day context. "
        "Reinforce-only: boosts with-trend reversals, never vetoes a counter-trend "
        "bottom or an exit."
    )
    params_model = TrendLocationParams
    kind = "directional"
    reinforce_only = True
    weight = 0.15

    def values(self, bars: pd.DataFrame, as_of: date) -> dict[str, float] | None:
        if "close" not in bars.columns:
            return None
        p: TrendLocationParams = self.params  # type: ignore[assignment]
        return _trend_values(
            bars["close"],
            pos50_band=p.pos50_band,
            pos200_band=p.pos200_band,
            slope_band=p.slope_band,
            slope_window=p.slope_window,
        )

    def score(self, values: dict[str, float], strategy: type[Strategy] | None) -> float:
        # Natural signed value; the composite applies the reinforce-only rule.
        return self.clamp(values["raw"])
