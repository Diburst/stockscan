"""Reversal trigger — exhaustion + the turn (spec §4.2).

The core of the reversal score and the tight-timeline piece. Fires only when the
fast oscillator (RSI(2), Connors' primitive) reached an extreme **and is now
hooking back** — being oversold is not enough; the turn must show in the last 1–2
bars. Folds depth-of-extreme and the hook (+ a confirming bar) into one signed
value: positive = bottom turn, negative = top turn. A name pinned deep-oversold
but still falling scores ~0 (no hook yet) — the "don't catch the knife mid-air"
discipline.

Pure math is in `_reversal_values` (bars-only, no DB, no look-ahead).
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

import pandas as pd
from pydantic import Field

from stockscan.indicators import rsi as compute_rsi
from stockscan.technical.indicators.base import (
    TechnicalIndicator,
    TechnicalIndicatorParams,
)

if TYPE_CHECKING:
    from stockscan.strategies.base import Strategy


def _clip(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _reversal_values(
    close: pd.Series, *, rsi_period: int, os: float, ob: float
) -> dict[str, float] | None:
    c = close.dropna()
    if len(c) < 15:
        return None
    rsi2 = compute_rsi(c, rsi_period)
    a, b = rsi2.iloc[-1], rsi2.iloc[-2]
    if pd.isna(a) or pd.isna(b):
        return None
    a, b = float(a), float(b)
    lo2, hi2 = min(a, b), max(a, b)
    last, prev = float(c.iloc[-1]), float(c.iloc[-2])

    # Bottom (bullish reversal): was oversold in the last 2 bars, now turning up.
    depth_b = _clip((os - lo2) / os, 0.0, 1.0)
    bull = depth_b * (0.5 * (a > b) + 0.5 * (last > prev)) if lo2 <= os else 0.0

    # Top (bearish reversal): mirror.
    depth_t = _clip((hi2 - ob) / (100.0 - ob), 0.0, 1.0)
    bear = depth_t * (0.5 * (a < b) + 0.5 * (last < prev)) if hi2 >= ob else 0.0

    return {
        "rsi2": round(a, 4),
        "rsi2_prev": round(b, 4),
        "raw": _clip(bull - bear),
    }


class ReversalTriggerParams(TechnicalIndicatorParams):
    rsi_period: int = Field(2, ge=2, le=14, description="Fast RSI period (Connors RSI(2)).")
    os: float = Field(10.0, ge=1, le=49, description="Oversold threshold.")
    ob: float = Field(90.0, ge=51, le=99, description="Overbought threshold.")


class TechnicalReversalTrigger(TechnicalIndicator):
    name = "reversal_trigger"
    description = (
        "RSI(2) at an extreme AND hooking back — the tight-timeline reversal turn. "
        "Positive = bottom turn, negative = top turn."
    )
    params_model = ReversalTriggerParams
    kind = "directional"
    weight = 0.35

    def values(self, bars: pd.DataFrame, as_of: date) -> dict[str, float] | None:
        if "close" not in bars.columns:
            return None
        p: ReversalTriggerParams = self.params  # type: ignore[assignment]
        return _reversal_values(bars["close"], rsi_period=p.rsi_period, os=p.os, ob=p.ob)

    def score(self, values: dict[str, float], strategy: type[Strategy] | None) -> float:
        # Inherently a reversal signal; signed value regardless of firing strategy.
        return self.clamp(values["raw"])
