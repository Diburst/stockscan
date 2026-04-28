"""Composite technical-score computation.

`compute_technical_score(strategy, bars, as_of)` orchestrates every
registered indicator. Each indicator either:
  - Returns raw values + a [-1, +1] confirmation score, contributing to the
    composite as one vote, or
  - Returns None (insufficient history or abstaining), in which case the
    composite skips it.

The composite is the equal-weight average of contributing indicators.
If every indicator abstains, the composite is None and the caller stores
nothing — callers handle the None as "no tech score available".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

import pandas as pd

from stockscan.strategies.base import Strategy
from stockscan.technical.indicators import (
    TECH_REGISTRY,
    discover_technical_indicators,
)


@dataclass(frozen=True, slots=True)
class TechnicalScore:
    """Composite + per-indicator breakdown."""

    score: float                                  # in [-1, +1]
    breakdown: dict[str, dict[str, Any]] = field(default_factory=dict)
    contributing: int = 0                          # how many indicators voted

    def to_breakdown_json(self) -> dict[str, Any]:
        return {"score": self.score, "indicators": self.breakdown}


def compute_technical_score(
    strategy: type[Strategy] | None,
    bars: pd.DataFrame,
    as_of: date,
) -> TechnicalScore | None:
    """Run every registered indicator; return composite + breakdown, or None
    if every indicator abstained.

    `strategy=None` triggers neutral / direction-agnostic scoring (used by
    the watchlist).
    """
    discover_technical_indicators()
    if not TECH_REGISTRY:
        return None

    breakdown: dict[str, dict[str, Any]] = {}
    scores: list[float] = []

    for indicator_cls in TECH_REGISTRY.all():
        instance = indicator_cls()
        try:
            values = instance.values(bars, as_of)
        except Exception:
            # An indicator failure shouldn't poison the whole composite.
            continue
        if values is None:
            continue
        try:
            sub_score = instance.score(values, strategy)
        except Exception:
            continue
        sub_score = max(-1.0, min(1.0, float(sub_score)))
        breakdown[indicator_cls.name] = {**values, "score": sub_score}
        scores.append(sub_score)

    if not scores:
        return None

    composite = sum(scores) / len(scores)
    return TechnicalScore(
        score=composite, breakdown=breakdown, contributing=len(scores)
    )
