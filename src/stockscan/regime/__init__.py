"""Market-regime detector — ADX + SMA(200) classifier on SPY."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from stockscan.regime.detect import classify_regime, detect_regime
from stockscan.regime.store import (
    MarketRegime,
    RegimeLabel,
    get_regime,
    latest_regime,
    upsert_regime,
)

if TYPE_CHECKING:
    from collections.abc import Iterable


def build_strategy_factors(
    regime: MarketRegime | None,
    strategies: Iterable[Any],
) -> list[dict[str, object]]:
    """Build the per-strategy sizing-multiplier breakdown the dashboard renders.

    Each entry has: ``cls``, ``affinity``, ``composite_mult``, ``stress_mult``,
    ``effective``. When ``regime`` is None we fall back to neutral defaults
    (affinity = strategy default, multipliers = 1.0) so the regime card and
    refresh handler can both call this without conditional branching.

    Pulled out of ``web/routes/dashboard.py`` so the regime-refresh route
    can reuse it after a forced recompute.
    """
    factors: list[dict[str, object]] = []
    if regime is not None:
        composite_dec = regime.composite_score
        composite = float(composite_dec) if composite_dec is not None else None
        composite_mult = 0.5 + 0.5 * composite if composite is not None else 1.0
        stress_mult = 0.5 if regime.credit_stress_flag else 1.0
        for cls in strategies:
            affinity = cls.affinity_for(regime.regime)
            effective = affinity * composite_mult * stress_mult
            factors.append(
                {
                    "cls": cls,
                    "affinity": affinity,
                    "composite_mult": composite_mult,
                    "stress_mult": stress_mult,
                    "effective": effective,
                }
            )
    else:
        for cls in strategies:
            factors.append(
                {
                    "cls": cls,
                    "affinity": cls.default_affinity,
                    "composite_mult": 1.0,
                    "stress_mult": 1.0,
                    "effective": 1.0,
                }
            )
    return factors


__all__ = [
    "MarketRegime",
    "RegimeLabel",
    "build_strategy_factors",
    "classify_regime",
    "detect_regime",
    "get_regime",
    "latest_regime",
    "upsert_regime",
]
