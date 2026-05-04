"""Per-symbol technical analysis engine.

For each watchlist symbol, computes a structured analysis aimed at
short-term (7-30 day) options trading:

  * **Support / resistance levels** - clustered swing highs and lows
    with strength scoring by touch count and recency.
  * **Trend** - multi-timeframe MA stack alignment + ADX strength bucket
    + recent returns.
  * **Volatility** - realized vol (21d, 63d), ATR(20), Bollinger band
    width, HV percentile vs trailing year, and forward-projected
    expected-range bands at ±1sigma for 7d and 30d horizons.
  * **Momentum** - RSI(14) + MACD signal/histogram with bucketed labels.
  * **Options context** - days-to-earnings, HV-percentile framing,
    current-price-vs-levels position with strike-selection hints.

All sub-modules are pure functions over a daily-bars DataFrame; the
:func:`analyze_symbol` orchestrator pulls bars from the local store
once and dispatches. Soft-fails per sub-module so one bad indicator
doesn't blank out the whole report.

The :mod:`stockscan.analysis.batch` runner iterates the watchlist and
returns a list of :class:`SymbolAnalysis` for the dashboard cards.
The :mod:`stockscan.analysis.chart` module renders an SVG price chart
per symbol with S/R levels and expected-range bands overlaid.
"""

from __future__ import annotations

from stockscan.analysis.batch import analyze_watchlist
from stockscan.analysis.chart import render_chart_svg
from stockscan.analysis.engine import analyze_symbol
from stockscan.analysis.state import (
    ExpectedRange,
    Level,
    MomentumState,
    OptionsContext,
    SymbolAnalysis,
    TrendState,
    VolatilityState,
)

__all__ = [
    "ExpectedRange",
    "Level",
    "MomentumState",
    "OptionsContext",
    "SymbolAnalysis",
    "TrendState",
    "VolatilityState",
    "analyze_symbol",
    "analyze_watchlist",
    "render_chart_svg",
]
