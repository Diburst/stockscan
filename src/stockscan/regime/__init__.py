"""Market regime — the trend gate, the volatility scalar and the credit-stress
breaker that govern new entries and position size.

:mod:`stockscan.regime.rules` holds the pure math, :mod:`~.detect` computes and
caches today's row, :mod:`~.store` persists it.
"""

from __future__ import annotations

from stockscan.regime.detect import detect_regime
from stockscan.regime.store import (
    MarketRegime,
    RegimeLabel,
    get_regime,
    latest_regime,
    regime_label,
    upsert_regime,
)

__all__ = [
    "MarketRegime",
    "RegimeLabel",
    "detect_regime",
    "get_regime",
    "latest_regime",
    "regime_label",
    "upsert_regime",
]
