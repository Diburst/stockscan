"""Index-level technical-structure indicators for the dashboard.

Three indicators, all computed from SPY's daily bars:

  * SPY ADX(14) — trend-strength state with bucketed labels.
  * SPY Bollinger %B (20, 2) — overbought / oversold positioning
    relative to the 20-day Bollinger Bands.
  * SPY BB width — current width as a percentile of its trailing
    six-month distribution. Low percentile = volatility compression
    (Crabel-style "coiled spring"); high percentile = expansion
    typically followed by mean reversion.

Each sub-state carries both a numeric reading AND a curated flavor
text so the dashboard can render not just the value but what THIS
particular reading means for trend-following vs mean-reversion
strategies.

Design intent matches the 'cycles' package: pure-functional
indicators that take a SPY-bars DataFrame and return a typed state
dataclass. Soft-fails per indicator with an ``available=False``
flag when the input data isn't sufficient.
"""

from __future__ import annotations

from stockscan.structure.state import (
    IndexStructureState,
    compute_index_structure,
)

__all__ = [
    "IndexStructureState",
    "compute_index_structure",
]
