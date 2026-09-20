"""Per-symbol technical analysis engine.

For each watchlist symbol, computes a structured analysis aimed at
short-term (7-30 day) options trading:

  * **Trend** - multi-timeframe MA stack alignment + recent returns.
  * **Volatility** - realized vol (21d, 63d), ATR(14), HV percentile
    vs trailing year, and forward-projected expected-range bands at
    ±1sigma for 7d and 30d horizons.
  * **Options context** - days-to-earnings, HV-percentile framing,
    Black-Scholes strike ladder with EMA-confluence hints.

All sub-modules are pure functions over a daily-bars DataFrame; the
:func:`analyze_symbol` orchestrator pulls bars from the local store
once and dispatches. Soft-fails per sub-module so one bad indicator
doesn't blank out the whole report.

The :mod:`stockscan.analysis.batch` runner iterates the watchlist and
returns a list of :class:`SymbolAnalysis` for the dashboard cards.
The :mod:`stockscan.analysis.chart` module renders an SVG price chart
per symbol with expected-range bands overlaid.
"""

from __future__ import annotations

from stockscan.analysis.batch import analyze_watchlist, analyze_watchlist_cards
from stockscan.analysis.chart import render_chart_svg
from stockscan.analysis.chart_data import build_chart_payload
from stockscan.analysis.engine import analyze_symbol
from stockscan.analysis.state import (
    ExpectedRange,
    OptionsContext,
    OptionStrike,
    StrikeSet,
    SymbolAnalysis,
    TrendState,
    VolatilityState,
)

__all__ = [
    "ExpectedRange",
    "OptionStrike",
    "OptionsContext",
    "StrikeSet",
    "SymbolAnalysis",
    "TrendState",
    "VolatilityState",
    "analyze_symbol",
    "analyze_watchlist",
    "analyze_watchlist_cards",
    "build_chart_payload",
    "render_chart_svg",
]
