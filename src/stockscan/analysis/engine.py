"""Per-symbol analysis orchestrator.

One entry point: :func:`analyze_symbol`. Pulls daily bars from the
local store (or accepts a pre-loaded DataFrame for testing), then
dispatches to each sub-module. Soft-fails per sub-module so a single
broken indicator doesn't blank out the whole report.
"""

from __future__ import annotations

import logging
from datetime import date as _date
from datetime import timedelta as _td
from typing import TYPE_CHECKING

from stockscan.analysis.levels import find_support_resistance
from stockscan.analysis.momentum import compute_momentum
from stockscan.analysis.options_context import compute_options_context
from stockscan.analysis.state import SymbolAnalysis
from stockscan.analysis.trend import compute_trend
from stockscan.analysis.volatility import compute_volatility
from stockscan.data.store import get_bars
from stockscan.db import session_scope

if TYPE_CHECKING:
    import pandas as pd
    from sqlalchemy.orm import Session

log = logging.getLogger(__name__)


# How many years of bars to pull. Need ≥1 year for HV percentile +
# ADX warmup; 2 years gives margin for the rolling-vol baseline.
_LOOKBACK_YEARS = 2

# Cap chart history sent to the SVG renderer at 1 year (252 trading
# days) so the chart isn't crowded.
_CHART_HISTORY_DAYS = 252


def analyze_symbol(
    symbol: str,
    *,
    as_of: _date | None = None,
    bars: pd.DataFrame | None = None,
    session: Session | None = None,
) -> SymbolAnalysis:
    """Run the full per-symbol analysis pipeline.

    Parameters
    ----------
    symbol:
        Ticker, e.g. 'AAPL'.
    as_of:
        Date the analysis is computed for. Default = today.
    bars:
        Optional pre-loaded bars DataFrame. When provided, skips the
        DB fetch - useful for tests and the batch runner that loads
        bars itself.
    session:
        Optional caller-managed DB session. When ``None``, this
        function opens its own.
    """
    if as_of is None:
        as_of = _date.today()

    if session is None:
        with session_scope() as s:
            return _analyze(symbol, as_of, bars, s)
    return _analyze(symbol, as_of, bars, session)


def _analyze(
    symbol: str,
    as_of: _date,
    bars: pd.DataFrame | None,
    session: Session,
) -> SymbolAnalysis:
    failures: list[str] = []

    # ---- Load bars if not provided ----
    if bars is None:
        try:
            start = as_of.replace(year=as_of.year - _LOOKBACK_YEARS)
        except ValueError:
            start = as_of.replace(year=as_of.year - _LOOKBACK_YEARS, month=2, day=28)
        try:
            bars = get_bars(symbol, start, as_of, session=session)
        except Exception as exc:
            log.warning("analysis: bars fetch failed for %s: %s", symbol, exc)
            return SymbolAnalysis.unavailable(symbol, as_of, "bars_fetch_failed")

    if bars is None or bars.empty:
        return SymbolAnalysis.unavailable(symbol, as_of, "no_bars")

    # The bars frame has columns symbol/open/high/low/close/adj_close/volume
    # and is indexed by bar_ts (UTC). Slice to <= as_of just in case.
    if hasattr(bars.index, "date"):
        bars = bars[bars.index.date <= as_of]
    if bars.empty:
        return SymbolAnalysis.unavailable(symbol, as_of, "no_bars_at_as_of")

    last_close = float(bars["close"].iloc[-1]) if "close" in bars.columns else None
    last_volume: float | None = None
    if "volume" in bars.columns and "close" in bars.columns:
        try:
            last_volume = float(bars["volume"].iloc[-1]) * float(bars["close"].iloc[-1])
        except (TypeError, ValueError):
            last_volume = None

    # ---- Sub-module dispatches with per-component soft-fail. ----
    levels = _safe_call(failures, "levels", lambda: find_support_resistance(bars))
    if levels is None:
        levels = []
    trend = _safe_call(failures, "trend", lambda: compute_trend(bars))
    volatility = _safe_call(failures, "volatility", lambda: compute_volatility(bars))
    momentum = _safe_call(failures, "momentum", lambda: compute_momentum(bars))

    options_ctx = _safe_call(
        failures, "options_context",
        lambda: compute_options_context(
            symbol=symbol, as_of=as_of, last_close=last_close,
            levels=levels, trend=trend, volatility=volatility,
            session=session,
        ),
    )

    # Fall back to "unavailable" defaults if any sub-call returned None.
    from stockscan.analysis.state import (
        MomentumState,
        OptionsContext,
        TrendState,
        VolatilityState,
    )
    if trend is None:
        trend = TrendState.unavailable()
    if volatility is None:
        volatility = VolatilityState.unavailable()
    if momentum is None:
        momentum = MomentumState.unavailable()
    if options_ctx is None:
        options_ctx = OptionsContext.unavailable()

    # ---- Build chart-history slices (chronological, capped). ----
    closes_history: list[tuple[_date, float]] = []
    volumes_history: list[tuple[_date, float]] = []
    if "close" in bars.columns:
        # Keep the last _CHART_HISTORY_DAYS bars.
        history = bars.iloc[-_CHART_HISTORY_DAYS:]
        for ts, row in history.iterrows():
            try:
                d = ts.date() if hasattr(ts, "date") else ts
                closes_history.append((d, float(row["close"])))
                if "volume" in row:
                    volumes_history.append(
                        (d, float(row["volume"]) * float(row["close"]))
                    )
            except (TypeError, ValueError):
                continue

    return SymbolAnalysis(
        symbol=symbol,
        as_of=as_of,
        available=True,
        last_close=last_close,
        last_volume=last_volume,
        bars_count=len(bars),
        levels=levels,
        trend=trend,
        volatility=volatility,
        momentum=momentum,
        options_context=options_ctx,
        closes_history=closes_history,
        volumes_history=volumes_history,
        failures=failures,
    )


def _safe_call(failures: list[str], name: str, fn):
    try:
        return fn()
    except Exception as exc:
        log.warning("analysis/%s: failed: %s", name, exc)
        failures.append(name)
        return None


# Module-load helper for "lookback start date" - exposed for the
# batch runner so it can preload bars in bulk before calling
# analyze_symbol per symbol.
def lookback_start(as_of: _date) -> _date:
    try:
        return as_of.replace(year=as_of.year - _LOOKBACK_YEARS)
    except ValueError:
        return as_of.replace(year=as_of.year - _LOOKBACK_YEARS, month=2, day=28) - _td(days=0)
