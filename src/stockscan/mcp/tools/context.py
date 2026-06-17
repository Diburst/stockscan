"""Market-context reads: fundamentals, screening, earnings, news, insider, econ."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from stockscan.earnings import earnings_in_window, latest_trend, next_earnings
from stockscan.econ_events.store import upcoming_events
from stockscan.fundamentals.store import get_fundamentals as _get_fundamentals
from stockscan.fundamentals.store import list_by_market_cap as _list_by_market_cap
from stockscan.insider.store import net_buys_90d, recent_transactions
from stockscan.mcp.serialize import jsonable
from stockscan.mcp.tools._provider import NoApiKeyError, provider_ctx
from stockscan.news.refresh import fetch_article_content
from stockscan.news.store import get_article as _get_article_row
from stockscan.news.store import recent_for_symbol, recent_general
from stockscan.watchlist import watchlist_symbols


# ----------------------------------------------------------- fundamentals
def get_fundamentals(symbol: str) -> dict[str, Any]:
    """Get the stored fundamentals snapshot for a symbol.

    Args:
        symbol: Ticker, e.g. "AAPL".

    Returns:
        The fundamentals row (name, sector, market_cap, pe_ratio, eps, beta,
        52-week high/low, ...), or {"error": "not_found"}.
    """
    f = _get_fundamentals(symbol)
    if f is None:
        return {"error": "not_found", "symbol": symbol.upper()}
    return jsonable(f)


def screen_by_market_cap(limit: int = 50) -> dict[str, Any]:
    """List the largest symbols by market cap (a simple size screen).

    Args:
        limit: How many to return, largest first (default 50).

    Returns:
        {"count", "symbols": [{symbol, name, sector, market_cap, ...}, ...]}.
    """
    rows = _list_by_market_cap(limit=limit)
    return {"count": len(rows), "symbols": [jsonable(r) for r in rows]}


# --------------------------------------------------------------- earnings
# Lean estimate fields kept from the (large) estimate-trend rows.
_ESTIMATE_KEYS = (
    "period",
    "period_end",
    "eps_estimate_avg",
    "eps_growth",
    "rev_estimate_avg",
    "rev_growth",
    "eps_analyst_count",
    "eps_revisions_up_30d",
    "eps_revisions_down_30d",
)


def _compact_estimate(trend_rows: list[Any]) -> dict[str, Any] | None:
    """Pick the nearest forward estimate row and keep only the lean fields.

    ``latest_trend`` returns every estimate-trend point (often ~100 rows); an
    agent only needs the upcoming period's consensus + revision momentum, so we
    reduce it to a single compact row.
    """
    if not trend_rows:
        return None
    nearest = min(trend_rows, key=lambda t: t.period_end)
    return {k: jsonable(getattr(nearest, k, None)) for k in _ESTIMATE_KEYS}


def get_earnings(symbol: str) -> dict[str, Any]:
    """Get a symbol's next earnings date and the nearest forward estimate.

    Args:
        symbol: Ticker, e.g. "AAPL".

    Returns:
        {"symbol", "next_earnings": {...}|None, "estimate": {period, period_end,
        eps/rev consensus + growth, analyst count, 30d revisions}|None}.
    """
    nxt = next_earnings(symbol)
    # Forward-looking estimate rows only (period_end >= today), then compacted.
    forward = latest_trend(symbol, since=date.today())
    return {
        "symbol": symbol.upper(),
        "next_earnings": jsonable(nxt),
        "estimate": _compact_estimate(forward),
    }


def upcoming_earnings(days: int = 14, list_id: int | None = None) -> dict[str, Any]:
    """List upcoming earnings for watched symbols within the next N days.

    Args:
        days: Forward window in days (default 14).
        list_id: Restrict to one watchlist list; None = all watched symbols.

    Returns:
        {"count", "window_days", "events": [{symbol, report_date, ...}, ...]}.
    """
    symbols = sorted(watchlist_symbols(list_id=list_id))
    if not symbols:
        return {"count": 0, "window_days": days, "events": []}
    today = date.today()
    events = earnings_in_window(symbols, start=today, end=today + timedelta(days=days))
    return {"count": len(events), "window_days": days, "events": [jsonable(e) for e in events]}


# ------------------------------------------------------------------- news
def get_news(symbol: str | None = None, limit: int = 10) -> dict[str, Any]:
    """Recent news — for one symbol, or the general configured feed.

    Args:
        symbol: Ticker to fetch news for; None = the general feed.
        limit: Max articles (default 10).

    Returns:
        {"count", "articles": [{title, link, published_at, symbols, ...}, ...]}.
    """
    articles = recent_for_symbol(symbol, limit=limit) if symbol else recent_general(limit=limit)
    return {"count": len(articles), "articles": [jsonable(a) for a in articles]}


def get_article(article_id: str) -> dict[str, Any]:
    """Get one article's metadata and re-fetch its full body on demand.

    The article_id comes from get_news. Bodies are not stored (only headlines/
    snippets/links), so this re-issues a tightly-scoped EODHD query to retrieve
    the full text — which costs ~1 API credit and needs EODHD_API_KEY. Without a
    key (or if the source no longer returns it) you still get the metadata, with
    content = null.

    Args:
        article_id: The article id from get_news.

    Returns:
        {"article": {...metadata...}, "content": str|None, "note"?: str}, or
        {"error": "not_found"}.
    """
    art = _get_article_row(article_id)
    if art is None:
        return {"error": "not_found", "article_id": article_id}
    out: dict[str, Any] = {"article": jsonable(art), "content": None}
    try:
        with provider_ctx() as provider:
            content = fetch_article_content(provider, art)
        out["content"] = content
        if content is None:
            out["note"] = "Full body not retrievable from the source right now."
    except NoApiKeyError:
        out["note"] = "EODHD_API_KEY not set — returning metadata only."
    return out


# ---------------------------------------------------------------- insider
def get_insider(symbol: str, lookback_days: int = 90, limit: int = 20) -> dict[str, Any]:
    """Get stored insider activity for a symbol: trailing net buys + recent txns.

    Reads already-stored data (use refresh_insider to pull fresh data — that
    costs API credits and has a cooldown).

    Args:
        symbol: Ticker, e.g. "AAPL".
        lookback_days: Trailing window for the net-buys aggregate (default 90).
        limit: Max recent transactions to return (default 20).

    Returns:
        {"symbol", "net_buys": {...}, "transactions": [...]}.
    """
    net = net_buys_90d(symbol, lookback_days=lookback_days)
    txns = recent_transactions(symbol, lookback_days=lookback_days, limit=limit)
    return {
        "symbol": symbol.upper(),
        "net_buys": jsonable(net),
        "transactions": [jsonable(t) for t in txns],
    }


def watchlist_insider(list_id: int | None = None, lookback_days: int = 90) -> dict[str, Any]:
    """Trailing insider net-buys for every watched symbol — a cross-section.

    The across-the-watchlist view of insider activity (per-symbol detail is in
    get_insider). Returns the net-buys aggregate per symbol, sorted by ticker.

    Args:
        list_id: Restrict to one watchlist list; None = all watched symbols.
        lookback_days: Trailing window for the aggregate (default 90).

    Returns:
        {"count", "lookback_days", "symbols": [{symbol, net_buys}, ...]}.
    """
    symbols = sorted(watchlist_symbols(list_id=list_id))
    rows = [
        {"symbol": s, "net_buys": jsonable(net_buys_90d(s, lookback_days=lookback_days))}
        for s in symbols
    ]
    return {"count": len(rows), "lookback_days": lookback_days, "symbols": rows}


# -------------------------------------------------------------- econ events
def upcoming_econ_events(
    days: int = 7, importance_min: str = "medium", limit: int = 100
) -> dict[str, Any]:
    """List upcoming US economic-calendar events within the next N days.

    Args:
        days: Forward window in days (default 7).
        importance_min: Minimum importance — "low", "medium", or "high".
        limit: Max events (default 100).

    Returns:
        {"count", "window_days", "events": [{event, date, importance, ...}, ...]}.
    """
    now = datetime.now()
    events = upcoming_events(
        start=now,
        end=now + timedelta(days=days),
        importance_min=importance_min,
        limit=limit,
    )
    return {"count": len(events), "window_days": days, "events": [jsonable(e) for e in events]}
