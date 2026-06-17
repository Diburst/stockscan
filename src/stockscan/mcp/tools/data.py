"""Data backfill + refresh tools (WRITE, external API). All hit EODHD.

These pull from the provider and cost API credits, so they live behind the
write-enable flag. They run synchronously (the call returns when the pull
finishes), so prefer passing an explicit, bounded symbol set. When no symbols
are given, the refresh tools fall back to the current watchlist. If
EODHD_API_KEY is unset they return {"error": "no_api_key"}.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from stockscan.data.backfill import backfill_symbol
from stockscan.earnings.refresh import refresh_earnings as _refresh_earnings
from stockscan.fundamentals.refresh import refresh_fundamentals as _refresh_fundamentals
from stockscan.insider.refresh import refresh_insider_for_symbol, refresh_insider_for_watchlist
from stockscan.mcp.serialize import jsonable
from stockscan.mcp.tools._provider import NoApiKeyError, provider_ctx
from stockscan.news.refresh import refresh_news as _refresh_news
from stockscan.universe.sp500 import refresh_universe as _refresh_universe
from stockscan.watchlist import watchlist_symbols

_SPLIT = re.compile(r"[\s,;]+")
_NO_KEY = {"error": "no_api_key", "detail": "EODHD_API_KEY is not set."}


def _parse_symbols(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [s.upper() for s in _SPLIT.split(raw.strip()) if s]


def _watchlist_or(parsed: list[str]) -> list[str]:
    return parsed or sorted(watchlist_symbols())


def backfill_bars(symbols: str, start: str | None = None) -> dict[str, Any]:
    """Backfill / incrementally update daily OHLCV bars for symbols. WRITE.

    Incremental: for a symbol already in the store it only tops up from the last
    stored bar (a few bars), so it's cheap to run for catch-up. For a brand-new
    symbol it fetches history back to ``start``. Pass an explicit symbol set
    (this does not default to the whole universe).

    Args:
        symbols: Whitespace/comma-separated tickers, e.g. "AAPL, MSFT NVDA".
        start: Earliest date (YYYY-MM-DD) to fetch for new symbols; defaults to
            ~5 years ago. Existing symbols ignore this beyond their last bar.

    Returns:
        {"ok": True, "results": {symbol: rows_upserted}, "total_upserted"}.
    """
    syms = _parse_symbols(symbols)
    if not syms:
        return {"error": "no_symbols", "detail": "Pass one or more tickers."}
    try:
        start_date = date.fromisoformat(start) if start else date.today() - timedelta(days=365 * 5)
    except ValueError:
        return {"error": "invalid_start", "detail": f"Bad date: {start!r} (use YYYY-MM-DD)."}
    try:
        results: dict[str, Any] = {}
        with provider_ctx() as provider:
            for sym in syms:
                try:
                    results[sym] = backfill_symbol(provider, sym, start=start_date)
                except Exception as exc:  # report per-symbol, keep going
                    results[sym] = {"error": str(exc)}
    except NoApiKeyError:
        return dict(_NO_KEY)
    total = sum(v for v in results.values() if isinstance(v, int))
    return {"ok": True, "results": results, "total_upserted": total}


def refresh_fundamentals(symbols: str | None = None) -> dict[str, Any]:
    """Pull fresh fundamentals for symbols (default: the watchlist). WRITE.

    Args:
        symbols: Whitespace/comma-separated tickers; empty = all watched symbols.

    Returns:
        {"ok": True, "status": {symbol: status}} or {"error": "no_api_key"}.
    """
    syms = _watchlist_or(_parse_symbols(symbols))
    if not syms:
        return {"error": "no_symbols", "detail": "No symbols given and watchlist is empty."}
    try:
        with provider_ctx() as provider:
            status = _refresh_fundamentals(provider, syms)
    except NoApiKeyError:
        return dict(_NO_KEY)
    return {"ok": True, "status": jsonable(status)}


def refresh_news(days_back: int = 7) -> dict[str, Any]:
    """Pull recent news for the general feed + watchlist symbols. WRITE.

    Args:
        days_back: How many days back to fetch (default 7).

    Returns:
        {"ok": True, "result": {...}} or {"error": "no_api_key"}.
    """
    syms = sorted(watchlist_symbols())
    try:
        with provider_ctx() as provider:
            result = _refresh_news(provider, days_back=days_back, watchlist_symbols=syms)
    except NoApiKeyError:
        return dict(_NO_KEY)
    return {"ok": True, "result": jsonable(result)}


def refresh_earnings(symbols: str | None = None, days_forward: int = 30) -> dict[str, Any]:
    """Refresh the earnings calendar + estimate trends for symbols. WRITE.

    Args:
        symbols: Whitespace/comma-separated tickers; empty = all watched symbols.
        days_forward: Calendar look-ahead in days (default 30).

    Returns:
        {"ok": True, "result": {...}} or {"error": "no_api_key"}.
    """
    syms = _watchlist_or(_parse_symbols(symbols))
    if not syms:
        return {"error": "no_symbols", "detail": "No symbols given and watchlist is empty."}
    try:
        with provider_ctx() as provider:
            result = _refresh_earnings(provider, syms, days_forward=days_forward)
    except NoApiKeyError:
        return dict(_NO_KEY)
    return {"ok": True, "result": jsonable(result)}


def refresh_insider(symbol: str | None = None) -> dict[str, Any]:
    """Pull fresh insider transactions. WRITE. Has a ~23h cooldown.

    Args:
        symbol: One ticker to refresh; omit to refresh the whole watchlist.

    Returns:
        {"ok": True, "result": {...}} or {"error": "no_api_key"}.
    """
    try:
        with provider_ctx() as provider:
            if symbol:
                result = refresh_insider_for_symbol(provider, symbol)
            else:
                result = refresh_insider_for_watchlist(provider)
    except NoApiKeyError:
        return dict(_NO_KEY)
    return {"ok": True, "result": jsonable(result)}


def refresh_universe() -> dict[str, Any]:
    """Refresh S&P 500 membership (historical + current) from the provider. WRITE.

    Returns:
        {"ok": True, "rows": N} or {"error": "no_api_key"}.
    """
    try:
        with provider_ctx() as provider:
            n = _refresh_universe(provider)
    except NoApiKeyError:
        return dict(_NO_KEY)
    return {"ok": True, "rows": n}
