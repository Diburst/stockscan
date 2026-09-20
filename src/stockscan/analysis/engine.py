"""Per-symbol analysis orchestrator.

One entry point: :func:`analyze_symbol`. Pulls daily bars from the
local store (or accepts a pre-loaded DataFrame for testing), then
dispatches to each sub-module. Soft-fails per sub-module so a single
broken indicator doesn't blank out the whole report.
"""

from __future__ import annotations

import logging
import math
from datetime import date as _date
from datetime import timedelta as _td
from typing import TYPE_CHECKING

from stockscan.analysis.options_context import compute_options_context
from stockscan.analysis.state import (
    OptionsContext,
    SymbolAnalysis,
    TrendState,
    VolatilityState,
)
from stockscan.analysis.trend import compute_trend
from stockscan.analysis.volatility import compute_volatility
from stockscan.data.store import get_bars
from stockscan.db import session_scope
from stockscan.indicators import avg_dollar_volume, sector_relative_return

if TYPE_CHECKING:
    import pandas as pd
    from sqlalchemy.orm import Session

log = logging.getLogger(__name__)


# How many years of bars to pull. Need ≥1 year for the HV percentile; the
# rolling-vol baselines all use trailing windows, so extra history never
# changes the computed indicators — it only feeds the chart.
# 3 years covers the longest selectable chart window (3y) on the Analysis
# hub and detail page.
_LOOKBACK_YEARS = 3

# Cap chart history at ~3 years (756 trading days) so the interactive
# candlestick mini-charts and the detail chart can offer a 3y window. The
# SVG renderer slices its own (shorter) window from this on top.
_CHART_HISTORY_DAYS = 756


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
    last_ts = bars.index[-1]
    last_bar_date = last_ts.date() if hasattr(last_ts, "date") else last_ts
    last_volume: float | None = None
    if "volume" in bars.columns and "close" in bars.columns:
        try:
            last_volume = float(bars["volume"].iloc[-1]) * float(bars["close"].iloc[-1])
        except (TypeError, ValueError):
            last_volume = None
    adv_20d = _adv_20d(bars)
    day_move_pct = _day_move_pct(bars)
    day_move_residual_pct = _day_move_residual_pct(symbol, bars, as_of, day_move_pct)

    # ---- Sub-module dispatches with per-component soft-fail. ----
    trend = _safe_call(failures, "trend", lambda: compute_trend(bars))
    volatility = _safe_call(failures, "volatility", lambda: compute_volatility(bars))
    if trend is None:
        trend = TrendState.unavailable()
    if volatility is None:
        volatility = VolatilityState.unavailable()
    daily_sigma_pct = _daily_sigma_pct(volatility)

    options_ctx = _safe_call(
        failures, "options_context",
        lambda: compute_options_context(
            symbol=symbol, as_of=as_of, last_close=last_close,
            trend=trend, volatility=volatility,
            session=session,
        ),
    )
    if options_ctx is None:
        options_ctx = OptionsContext.unavailable()

    # ---- Build chart-history slices (chronological, capped). ----
    closes_history: list[tuple[_date, float]] = []
    volumes_history: list[tuple[_date, float]] = []
    ohlc_history: list[dict[str, float | str]] = []
    if "close" in bars.columns:
        # Keep the last _CHART_HISTORY_DAYS bars.
        history = bars.iloc[-_CHART_HISTORY_DAYS:]
        has_ohlc = all(c in history.columns for c in ("open", "high", "low"))
        for ts, row in history.iterrows():
            try:
                d = ts.date() if hasattr(ts, "date") else ts
                close_v = float(row["close"])
                closes_history.append((d, close_v))
                if "volume" in row:
                    volumes_history.append((d, float(row["volume"]) * close_v))
                if has_ohlc:
                    ohlc_history.append(
                        {
                            "time": d.isoformat(),
                            "open": float(row["open"]),
                            "high": float(row["high"]),
                            "low": float(row["low"]),
                            "close": close_v,
                            "volume": (
                                float(row["volume"]) if "volume" in row else 0.0
                            ),
                        }
                    )
            except (TypeError, ValueError):
                continue

    return SymbolAnalysis(
        symbol=symbol,
        as_of=as_of,
        available=True,
        last_close=last_close,
        last_bar_date=last_bar_date,
        last_volume=last_volume,
        bars_count=len(bars),
        trend=trend,
        volatility=volatility,
        options_context=options_ctx,
        adv_20d=adv_20d,
        day_move_pct=day_move_pct,
        day_move_residual_pct=day_move_residual_pct,
        daily_sigma_pct=daily_sigma_pct,
        closes_history=closes_history,
        volumes_history=volumes_history,
        ohlc_history=ohlc_history,
        failures=failures,
    )


def _adv_20d(bars: pd.DataFrame) -> float | None:
    """Mean close × volume over the last 20 bars; None with fewer bars."""
    if "close" not in bars.columns or "volume" not in bars.columns:
        return None
    adv = float(avg_dollar_volume(bars["close"], bars["volume"], period=20).iloc[-1])
    return None if math.isnan(adv) else adv


def _day_move_pct(bars: pd.DataFrame) -> float | None:
    """1-day % change of adj_close; None with fewer than two bars."""
    if "adj_close" not in bars.columns or len(bars) < 2:
        return None
    prev, last = float(bars["adj_close"].iloc[-2]), float(bars["adj_close"].iloc[-1])
    if not prev or math.isnan(prev) or math.isnan(last):
        return None
    return (last - prev) / prev * 100.0


def _day_move_residual_pct(
    symbol: str, bars: pd.DataFrame, as_of: _date, day_move_pct: float | None
) -> float | None:
    """``day_move_pct`` net of the sector composite's 1-day return.

    None when the symbol has no sector composite or the composites are not
    built; a missing composite must never break the analysis page.
    """
    if day_move_pct is None:
        return None
    bars.attrs["symbol"] = symbol
    try:
        residual = sector_relative_return(bars, as_of, lookback=1)
    except Exception as exc:
        log.debug("analysis: sector residual failed for %s: %s", symbol, exc)
        return None
    return None if residual is None else residual * 100.0


def _daily_sigma_pct(volatility: VolatilityState) -> float | None:
    """One trading day of vol in %, from the annualised EWMA (else 21d) vol."""
    annual = volatility.ewma_vol_pct or volatility.realized_vol_21d_pct
    return None if annual is None else annual / math.sqrt(252)


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
