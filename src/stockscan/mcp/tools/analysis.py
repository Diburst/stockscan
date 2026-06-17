"""Per-symbol analysis, market regime, and the watchlist-wide cross-section."""

from __future__ import annotations

from datetime import date
from typing import Any

from stockscan.analysis import analyze_symbol
from stockscan.analysis import analyze_watchlist as _analyze_watchlist
from stockscan.mcp.serialize import jsonable
from stockscan.regime import get_regime as _get_regime

FACETS = (
    "summary",
    "trend",
    "volatility",
    "momentum",
    "levels",
    "options_summary",
    "options_context",
    "full",
)

# Per-bar chart series carried on SymbolAnalysis for the web UI's candlestick
# charts. They are large (hundreds of rows each) and useless to an agent, so we
# strip them from every MCP response — request bars elsewhere if ever needed.
_CHART_HISTORY_KEYS = ("ohlc_history", "closes_history", "volumes_history")


def _strip_chart_history(d: dict[str, Any]) -> dict[str, Any]:
    """Drop the bulky per-bar chart arrays from a serialized SymbolAnalysis."""
    for key in _CHART_HISTORY_KEYS:
        d.pop(key, None)
    return d


def get_analysis(symbol: str, as_of: str | None = None) -> dict[str, Any]:
    """Run the full technical-analysis pipeline for one symbol.

    Covers support/resistance levels, trend state, volatility, momentum
    (RSI/MACD), and an options/expected-move context.

    Args:
        symbol: Ticker, e.g. "AAPL".
        as_of: ISO date (YYYY-MM-DD) to analyze as of; default today.

    Returns:
        The SymbolAnalysis as a dict (minus the per-bar chart-history arrays,
        which are web-UI only), or an "unavailable" payload if bars are missing.
    """
    res = analyze_symbol(symbol, as_of=date.fromisoformat(as_of) if as_of else None)
    out = jsonable(res)
    return _strip_chart_history(out) if isinstance(out, dict) else out


def _project(a: Any, facet: str) -> dict[str, Any]:
    """Compress one SymbolAnalysis to the requested facet (keeps payloads small)."""
    base: dict[str, Any] = {
        "symbol": a.symbol,
        "available": a.available,
        "last_close": jsonable(a.last_close),
    }
    if not a.available:
        base["reason"] = list(getattr(a, "failures", []) or []) or "unavailable"
        return base
    if facet == "summary":
        base["trend_bucket"] = a.trend.bucket
        base["vol_bucket"] = a.volatility.bucket
        base["rsi_bucket"] = a.momentum.rsi_bucket
        base["macd_state"] = a.momentum.macd_state
        return base
    if facet == "options_summary":
        oc = a.options_context
        sets = list(getattr(oc, "strike_sets", None) or [])
        nearest = sets[0] if sets else None

        def _leg(side: str) -> dict[str, Any] | None:
            leg = getattr(nearest, side, None) if nearest is not None else None
            if leg is None:
                return None
            return {"strike": leg.strike, "pct_otm": round(leg.pct_otm, 1)}

        iv = None
        n_conf = 0
        if nearest is not None:
            if nearest.call is not None:
                iv = nearest.call.vol_pct
                n_conf += len(nearest.call.confluences)
            if nearest.put is not None:
                if iv is None:
                    iv = nearest.put.vol_pct
                n_conf += len(nearest.put.confluences)
        base["iv_pct"] = round(iv) if iv is not None else None
        base["days_to_earnings"] = oc.days_to_earnings
        base["earnings_warning"] = oc.earnings_warning
        base["nearest_expiry"] = (
            {
                "days_to_expiry": nearest.days_to_expiry,
                "expiry_date": jsonable(nearest.expiry_date),
            }
            if nearest is not None
            else None
        )
        base["call_15d"] = _leg("call")
        base["put_15d"] = _leg("put")
        base["pct_to_resistance"] = (
            round(oc.pct_to_resistance, 1) if oc.pct_to_resistance is not None else None
        )
        base["pct_to_support"] = (
            round(oc.pct_to_support, 1) if oc.pct_to_support is not None else None
        )
        base["confluence_count"] = n_conf
        return base
    if facet == "trend":
        base["trend"] = jsonable(a.trend)
    elif facet == "volatility":
        base["volatility"] = jsonable(a.volatility)
    elif facet == "momentum":
        base["momentum"] = jsonable(a.momentum)
    elif facet == "levels":
        base["levels"] = jsonable(a.levels)
    elif facet == "options_context":
        base["options_context"] = jsonable(a.options_context)
    elif facet == "full":
        return _strip_chart_history(jsonable(a))
    return base


def analyze_watchlist(
    list_id: int | None = None,
    facet: str = "summary",
    as_of: str | None = None,
) -> dict[str, Any]:
    """Run technical analysis across every watched symbol — a cross-section.

    This runs the full per-symbol pipeline for each symbol on the watchlist, so
    it can be slow for large lists. To keep the response small, it returns only
    the requested ``facet`` of each symbol's analysis by default.

    Args:
        list_id: Restrict to one named list (see list_watchlists); None = all.
        facet: Which slice to return per symbol. One of: "summary" (default —
            last_close + trend/vol/rsi/macd buckets), "trend", "volatility",
            "momentum", "levels", "options_summary" (lean options view — IV,
            earnings flag, nearest 15-delta call/put strikes, support/resistance
            distance, confluence count; preferred for an options cross-section),
            "options_context" (full strike sets with greeks + confluences —
            large), or "full" (everything — largest; use sparingly).
        as_of: ISO date (YYYY-MM-DD); default today.

    Returns:
        {"facet", "count", "as_of", "symbols": [<projected analysis>, ...]}.
    """
    if facet not in FACETS:
        return {"error": "unknown_facet", "facet": facet, "valid": list(FACETS)}
    as_of_d = date.fromisoformat(as_of) if as_of else date.today()
    analyses = _analyze_watchlist(as_of=as_of_d, list_id=list_id)
    return {
        "facet": facet,
        "count": len(analyses),
        "as_of": as_of_d.isoformat(),
        "symbols": [_project(a, facet) for a in analyses],
    }


def get_regime(as_of: str | None = None) -> dict[str, Any]:
    """Get the detected market regime and composite-health scores.

    Args:
        as_of: ISO date (YYYY-MM-DD); default today.

    Returns:
        The MarketRegime row (regime label, ADX, composite/vol/trend/breadth/
        credit scores, credit-stress flag), or {"error": "no_regime"}.
    """
    as_of_d = date.fromisoformat(as_of) if as_of else date.today()
    reg = _get_regime(as_of_d)
    if reg is None:
        return {"error": "no_regime", "as_of": as_of_d.isoformat()}
    return jsonable(reg)
