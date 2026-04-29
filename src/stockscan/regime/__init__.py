"""Market-regime detector — ADX + SMA(200) classifier on SPY."""

from stockscan.regime.detect import classify_regime, detect_regime
from stockscan.regime.store import (
    MarketRegime,
    RegimeLabel,
    get_regime,
    latest_regime,
    upsert_regime,
)

__all__ = [
    "MarketRegime",
    "RegimeLabel",
    "classify_regime",
    "detect_regime",
    "get_regime",
    "latest_regime",
    "upsert_regime",
]
