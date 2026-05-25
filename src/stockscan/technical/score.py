"""Composite technical-score computation — v2 reversal model (spec §6).

`compute_technical_score(strategy, bars, as_of)` produces a **signed** reversal
score in [-1, +1]: ``+1`` = high-conviction bottom (long), ``-1`` = high-conviction
top (exit/short). It runs every registered indicator with ``in_score=True`` and
combines them in two stages:

  Stage 1 — directional core: weighted average of the core directional votes
            (``reversal_trigger``, ``pivot_proximity``, ``sector_rs``).
            ``trend_location`` is a **reinforce-only** directional input: it only
            adds conviction when its sign agrees with the core; when it would
            oppose, it abstains (never vetoes a counter-trend bottom or an exit).

  Stage 2 — confirmation attenuation: the directional composite D is multiplied
            by the product of confirmation multipliers (``volume_confirm``), each
            in [VOL_FLOOR, 1]. Confirmation can only scale |D|, never flip its
            sign. ``S = clip(D * C, -1, +1)``.

If every directional indicator abstains, returns None (caller stores nothing).
Legacy indicators (rsi/macd) set ``in_score=False`` and are excluded here while
remaining registered + directly testable.
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

METHODOLOGY_VERSION = 2


@dataclass(frozen=True, slots=True)
class TechnicalScore:
    """Composite + per-indicator breakdown."""

    score: float                                  # signed, in [-1, +1]
    breakdown: dict[str, dict[str, Any]] = field(default_factory=dict)
    contributing: int = 0                          # how many indicators voted

    def to_breakdown_json(self) -> dict[str, Any]:
        return {"score": self.score, "indicators": self.breakdown}


def _sign(x: float) -> int:
    return (x > 0) - (x < 0)


def compute_technical_score(
    strategy: type[Strategy] | None,
    bars: pd.DataFrame,
    as_of: date,
) -> TechnicalScore | None:
    """Signed reversal score in [-1,+1], or None if every directional indicator
    abstained. ``strategy=None`` triggers neutral scoring (watchlist)."""
    discover_technical_indicators()
    if not TECH_REGISTRY:
        return None

    breakdown: dict[str, dict[str, Any]] = {}
    core: list[tuple[float, float]] = []   # (weight, sub_score) — core directional
    trend: list[tuple[float, float]] = []  # (weight, sub_score) — reinforce-only
    mults: list[float] = []                # confirmation multipliers

    for indicator_cls in TECH_REGISTRY.all():
        if not getattr(indicator_cls, "in_score", True):
            continue
        instance = indicator_cls()
        try:
            values = instance.values(bars, as_of)
        except Exception:
            # An indicator failure shouldn't poison the whole composite.
            continue
        if values is None:
            continue
        try:
            sub = float(instance.score(values, strategy))
        except Exception:
            continue

        if getattr(indicator_cls, "kind", "directional") == "confirmation":
            m = max(0.0, min(1.0, sub))
            mults.append(m)
            breakdown[indicator_cls.name] = {**values, "multiplier": m}
        else:
            s = max(-1.0, min(1.0, sub))
            w = float(getattr(indicator_cls, "weight", 1.0))
            if getattr(indicator_cls, "reinforce_only", False):
                trend.append((w, s))
            else:
                core.append((w, s))
            breakdown[indicator_cls.name] = {**values, "score": s, "weight": w}

    if not core and not trend:
        return None

    # Stage 1a — core directional composite.
    core_den = sum(w for w, _ in core)
    core_num = sum(w * s for w, s in core)
    core_val = core_num / core_den if core_den > 0 else None
    if core_val is None:
        # Only reinforce-only inputs present → no reversal direction to reinforce.
        return None

    # Stage 1b — reinforce-only trend: counts only when it agrees with core.
    D = core_val
    if trend:
        trend_den = sum(w for w, _ in trend)
        trend_num = sum(w * s for w, s in trend)
        trend_val = trend_num / trend_den if trend_den > 0 else 0.0
        if _sign(trend_val) != 0 and _sign(trend_val) == _sign(core_val):
            D = (core_num + trend_num) / (core_den + trend_den)

    # Stage 2 — confirmation attenuation.
    C = 1.0
    for m in mults:
        C *= m

    S = max(-1.0, min(1.0, D * C))
    breakdown["_meta"] = {
        "D": round(D, 6),
        "C": round(C, 6),
        "score": round(S, 6),
        "methodology_version": METHODOLOGY_VERSION,
    }
    return TechnicalScore(
        score=S, breakdown=breakdown, contributing=len(core) + len(trend) + len(mults)
    )
