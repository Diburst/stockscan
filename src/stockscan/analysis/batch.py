"""Batch analysis runner - every watched symbol in one call.

The dashboard's 'Run analysis on watched' button hits the
:func:`analyze_watchlist` entry point. Each per-symbol analysis is
wrapped in a try/except so one bad symbol doesn't break the page;
failures are recorded as ``available=False`` rows.
"""

from __future__ import annotations

import logging
from datetime import date as _date
from typing import TYPE_CHECKING, Any

from stockscan.analysis.engine import analyze_symbol, lookback_start
from stockscan.analysis.state import SymbolAnalysis
from stockscan.db import session_scope
from stockscan.watchlist import watchlist_symbols

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

log = logging.getLogger(__name__)


def analyze_watchlist(
    *,
    as_of: _date | None = None,
    list_id: int | None = None,
    session: Session | None = None,
) -> list[SymbolAnalysis]:
    """Run :func:`analyze_symbol` for every symbol on the watchlist.

    ``list_id`` restricts to one named list; ``None`` analyses every symbol
    across all lists (the "All" view)."""
    if as_of is None:
        as_of = _date.today()
    if session is None:
        with session_scope() as s:
            return _run(s, as_of, list_id)
    return _run(session, as_of, list_id)


def _run(
    session: Session, as_of: _date, list_id: int | None = None
) -> list[SymbolAnalysis]:
    try:
        symbols = watchlist_symbols(list_id=list_id, session=session)
    except Exception as exc:
        log.warning("analyze_watchlist: watchlist lookup failed: %s", exc)
        return []
    # Alphabetical A→Z. watchlist_symbols returns a set (unordered);
    # sort so the analysis hub renders in a stable, predictable order
    # that matches the watchlist page.
    out: list[SymbolAnalysis] = []
    for sym in sorted(symbols):
        try:
            out.append(analyze_symbol(sym, as_of=as_of, session=session))
        except Exception as exc:
            log.warning("analyze_watchlist: %s failed: %s", sym, exc)
            out.append(SymbolAnalysis.unavailable(sym, as_of, f"engine_error: {exc}"))
    return out


def analyze_watchlist_cards(
    *,
    as_of: _date | None = None,
    list_id: int | None = None,
    session: Session | None = None,
) -> list[dict[str, Any]]:
    """Like :func:`analyze_watchlist`, but each item also carries the full
    interactive-chart payload so the Analysis hub can render fully-featured
    charts (every study, S/R levels, expected-move bands, fib).

    Returns ``[{"analysis": SymbolAnalysis, "payload": dict}, ...]``. Bars are
    fetched once per symbol and shared between the analysis engine and the
    chart-payload builder, so this is a single DB read per name rather than
    two. Soft-fails per symbol — one bad name renders as an unavailable card.
    """
    if as_of is None:
        as_of = _date.today()
    if session is None:
        with session_scope() as s:
            return _run_cards(s, as_of, list_id)
    return _run_cards(session, as_of, list_id)


def _run_cards(
    session: Session, as_of: _date, list_id: int | None
) -> list[dict[str, Any]]:
    # Lazy imports: keep the analyze_watchlist-only path free of chart/data
    # plumbing, mirroring engine.analyze_symbol's own lazy get_bars import.
    from stockscan.analysis.chart_data import build_chart_payload
    from stockscan.data.store import get_bars

    try:
        symbols = watchlist_symbols(list_id=list_id, session=session)
    except Exception as exc:
        log.warning("analyze_watchlist_cards: watchlist lookup failed: %s", exc)
        return []

    start = lookback_start(as_of)
    cards: list[dict[str, Any]] = []
    for sym in sorted(symbols):
        try:
            bars = get_bars(sym, start, as_of, session=session)
        except Exception as exc:
            log.warning("analyze_watchlist_cards: get_bars[%s] failed: %s", sym, exc)
            bars = None
        try:
            analysis = analyze_symbol(sym, as_of=as_of, bars=bars, session=session)
        except Exception as exc:
            log.warning("analyze_watchlist_cards: %s failed: %s", sym, exc)
            analysis = SymbolAnalysis.unavailable(sym, as_of, f"engine_error: {exc}")
        payload: dict[str, Any] = {}
        try:
            payload = build_chart_payload(sym, analysis, bars=bars, session=session)
        except Exception as exc:
            log.warning("analyze_watchlist_cards: payload[%s] failed: %s", sym, exc)
        cards.append({"analysis": analysis, "payload": payload})
    return cards
