"""Interactive-chart data payload for the /analysis/{symbol} detail page.

Returns a JSON-serializable dict containing:

  * ``bars``        — chronological OHLCV in Lightweight-Charts format.
  * ``studies``     — every selectable indicator (SMAs, EMAs, ATR bands,
                      volume), pre-computed so toggling on the client
                      never round-trips.
  * ``expected_move`` — forward ±1σ bands (7d / 30d) from
                        :class:`VolatilityState.expected_*`.
  * ``default_on``  — which studies/overlays are visible on first open.
                      User selections override via ``localStorage``.

Pure-data; no rendering. The view layer (Lightweight Charts in
``analysis/detail.html``) consumes the dict via ``|tojson``.

Note on the indicator surface area: this module only *composes* primitives
from :mod:`stockscan.indicators.ta` — there is no new indicator math
here. If a study is missing, add the corresponding primitive in
``indicators/ta.py`` (or wherever the canonical home lives) and then
expose it here. Per the project's "indicators are pure functions" rule,
this module must never grow its own math.
"""

from __future__ import annotations

import logging
from datetime import date as _date
from typing import TYPE_CHECKING, Any

import pandas as pd

from stockscan.analysis.state import SymbolAnalysis
from stockscan.indicators import ta

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

log = logging.getLogger(__name__)


# Cap chart history at ~756 trading days (~3 years) so the detail chart's
# range buttons can offer a 3y window. The default visible range stays short
# (the client fits to a recent window on load); the user widens via the
# 7d/30d/90d/1y/3y buttons or by scrolling out.
_CHART_HISTORY_DAYS = 756

# Studies on by default when the user first lands on /analysis/{symbol}.
# Per Thomas's selection: 50 SMA, 200 SMA, expected-move bands.
# Volume is always shown — it's part of the candle pane convention.
DEFAULT_STUDIES: tuple[str, ...] = (
    "sma_50",
    "sma_200",
    "volume",
    "expected_move",
)


def _series_to_lwc(series: pd.Series, dates: list[str]) -> list[dict[str, Any]]:
    """Convert a pandas Series to Lightweight-Charts ``[{time, value}, …]``.

    NaNs are skipped — Lightweight Charts treats missing bars as gaps,
    which is the correct behavior for warm-up rows on long-period MAs.
    """
    values = series.to_numpy(dtype=float)
    out: list[dict[str, Any]] = []
    for t, v in zip(dates, values, strict=True):
        if v != v:  # NaN check (faster than math.isnan in a tight loop)
            continue
        out.append({"time": t, "value": float(v)})
    return out


def _bars_to_lwc(bars: pd.DataFrame, dates: list[str]) -> list[dict[str, Any]]:
    """Convert an OHLCV DataFrame to Lightweight-Charts candle records."""
    o = bars["open"].to_numpy(dtype=float)
    h = bars["high"].to_numpy(dtype=float)
    low = bars["low"].to_numpy(dtype=float)
    c = bars["close"].to_numpy(dtype=float)
    v = bars["volume"].to_numpy(dtype=float)
    out: list[dict[str, Any]] = []
    for i, t in enumerate(dates):
        out.append(
            {
                "time": t,
                "open": float(o[i]),
                "high": float(h[i]),
                "low": float(low[i]),
                "close": float(c[i]),
                "volume": float(v[i]),
            }
        )
    return out


def build_chart_payload(
    symbol: str,
    analysis: SymbolAnalysis,
    *,
    session: Session | None = None,
    bars: pd.DataFrame | None = None,
) -> dict[str, Any]:
    """Build the interactive-chart payload for one symbol.

    Parameters
    ----------
    symbol:
        Ticker.
    analysis:
        The already-computed :class:`SymbolAnalysis` for this symbol;
        gives us the expected-move bands without re-running the engine.
    session:
        Optional DB session for the bars fetch.
    bars:
        Optional pre-loaded bars DataFrame (mainly for tests). When
        ``None``, pulls fresh via :func:`stockscan.data.store.get_bars`.

    Returns
    -------
    dict
        JSON-serializable payload. ``{}`` if no bars are available
        (the template renders the existing "no bars" fallback).
    """
    if bars is None:
        # Lazy import to keep the analyze_symbol-only test path free of
        # any DB plumbing.
        from stockscan.data.store import get_bars

        as_of = analysis.as_of or _date.today()
        try:
            start = as_of.replace(year=as_of.year - 3)
        except ValueError:  # Feb 29 → Feb 28 three years back
            start = as_of.replace(year=as_of.year - 3, month=2, day=28)
        try:
            bars = get_bars(symbol, start, as_of, session=session)
        except Exception as exc:
            log.warning("chart_data: get_bars failed for %s: %s", symbol, exc)
            return {}

    if bars is None or bars.empty:
        return {}

    # Trim future rows (defensive) and cap to the chart-history window.
    if hasattr(bars.index, "date"):
        bars = bars[bars.index.date <= analysis.as_of]
    bars = bars.iloc[-_CHART_HISTORY_DAYS:]
    if bars.empty:
        return {}

    # Pre-stringify the index once — every series re-uses this list.
    dates: list[str] = [
        (ts.date() if hasattr(ts, "date") else ts).isoformat()
        for ts in bars.index
    ]

    close = bars["close"]
    high = bars["high"]
    low = bars["low"]

    # ---- Overlays (price-pane) ---------------------------------------------
    # All studies follow the same shape: a {label, kind, data, color} dict.
    # ``kind`` tells the client renderer which Lightweight Charts series type
    # to use ("line", "band", "subpanel_volume").
    studies: dict[str, dict[str, Any]] = {}

    # Simple / exponential moving averages, common 20 / 50 / 200 grid.
    studies["sma_20"] = {
        "label": "SMA(20)", "kind": "line",
        "color": "#3b82f6",  # blue-500
        "data": _series_to_lwc(ta.sma(close, 20), dates),
    }
    studies["sma_50"] = {
        "label": "SMA(50)", "kind": "line",
        "color": "#0ea5e9",  # sky-500
        "data": _series_to_lwc(ta.sma(close, 50), dates),
    }
    studies["sma_200"] = {
        "label": "SMA(200)", "kind": "line",
        "color": "#0f172a",  # slate-900
        "data": _series_to_lwc(ta.sma(close, 200), dates),
    }
    studies["ema_20"] = {
        "label": "EMA(20)", "kind": "line",
        "color": "#8b5cf6",  # violet-500
        "data": _series_to_lwc(ta.ema(close, 20), dates),
    }
    studies["ema_50"] = {
        "label": "EMA(50)", "kind": "line",
        "color": "#a855f7",  # purple-500
        "data": _series_to_lwc(ta.ema(close, 50), dates),
    }
    studies["ema_200"] = {
        "label": "EMA(200)", "kind": "line",
        "color": "#7c3aed",  # violet-600
        "data": _series_to_lwc(ta.ema(close, 200), dates),
    }

    # ATR(14) × 2 bands around close. Close ± 2×ATR is the canonical "envelope"
    # for an ATR-based stop / range visualization.
    atr14 = ta.atr(high, low, close, period=14)
    atr_upper = close + 2 * atr14
    atr_lower = close - 2 * atr14
    studies["atr_bands"] = {
        "label": "ATR(14) ±2× bands", "kind": "band",
        "color": "#fbbf24",  # amber-400
        "upper": _series_to_lwc(atr_upper, dates),
        "middle": _series_to_lwc(close, dates),
        "lower": _series_to_lwc(atr_lower, dates),
    }

    # ---- Subpanels ---------------------------------------------------------
    # Volume — always present; the toggle just shows/hides it. The histogram
    # is colored by candle direction (up vs. prior close).
    volume_recs: list[dict[str, Any]] = []
    v_arr = bars["volume"].to_numpy(dtype=float)
    c_arr = close.to_numpy(dtype=float)
    o_arr = bars["open"].to_numpy(dtype=float)
    for i, t in enumerate(dates):
        up = c_arr[i] >= o_arr[i]
        volume_recs.append(
            {
                "time": t,
                "value": float(v_arr[i]),
                "color": "rgba(5,150,105,0.5)" if up else "rgba(220,38,38,0.5)",
            }
        )
    studies["volume"] = {
        "label": "Volume", "kind": "subpanel_volume",
        "color": "#94a3b8",
        "data": volume_recs,
    }

    # ---- Expected-move bands (forward ±1σ projections) ---------------------
    expected_move: dict[str, dict[str, Any]] = {}
    if analysis.volatility.expected_7d is not None:
        er = analysis.volatility.expected_7d
        expected_move["7d"] = {
            "horizon_days": er.horizon_days,
            "low": float(er.low),
            "high": float(er.high),
            "sigma_pct": float(er.sigma_pct),
        }
    if analysis.volatility.expected_30d is not None:
        er = analysis.volatility.expected_30d
        expected_move["30d"] = {
            "horizon_days": er.horizon_days,
            "low": float(er.low),
            "high": float(er.high),
            "sigma_pct": float(er.sigma_pct),
        }

    return {
        "symbol": symbol,
        "as_of": analysis.as_of.isoformat() if analysis.as_of else None,
        "last_close": (
            float(analysis.last_close) if analysis.last_close is not None else None
        ),
        "bars": _bars_to_lwc(bars, dates),
        "studies": studies,
        "expected_move": expected_move,
        "default_on": list(DEFAULT_STUDIES),
    }
