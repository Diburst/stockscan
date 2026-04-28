"""Per-signal technical confirmation scoring.

Each registered indicator interprets its raw values in light of the firing
strategy's tags ("mean_reversion", "trend_following", "breakout", ...) and
returns a confirmation score in [-1, +1]. The composite is the equal-weight
average across registered indicators that produced a value (insufficient
history is silently skipped).

To add a new indicator: drop a file in `technical/indicators/` that
subclasses `TechnicalIndicator` and declares per-tag scoring branches.
The registry auto-discovers it on the next process restart — same pattern
as the strategy plugin system.
"""

from stockscan.technical.indicators import (
    TECH_REGISTRY,
    TechnicalIndicator,
    TechnicalIndicatorParams,
    discover_technical_indicators,
)
from stockscan.technical.score import TechnicalScore, compute_technical_score
from stockscan.technical.store import upsert_score

__all__ = [
    "TECH_REGISTRY",
    "TechnicalIndicator",
    "TechnicalIndicatorParams",
    "TechnicalScore",
    "compute_technical_score",
    "discover_technical_indicators",
    "upsert_score",
]
