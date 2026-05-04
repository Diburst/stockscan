"""Batch analysis runner - every watched symbol in one call.

The dashboard's 'Run analysis on watched' button hits the
:func:`analyze_watchlist` entry point. Each per-symbol analysis is
wrapped in a try/except so one bad symbol doesn't break the page;
failures are recorded as ``available=False`` rows.
"""

from __future__ import annotations

import logging
from datetime import date as _date
from typing import TYPE_CHECKING

from stockscan.analysis.engine import analyze_symbol
from stockscan.analysis.state import SymbolAnalysis
from stockscan.db import session_scope
from stockscan.watchlist import watchlist_symbols

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

log = logging.getLogger(__name__)


def analyze_watchlist(
    *,
    as_of: _date | None = None,
    session: Session | None = None,
) -> list[SymbolAnalysis]:
    """Run :func:`analyze_symbol` for every symbol on the watchlist."""
    if as_of is None:
        as_of = _date.today()
    if session is None:
        with session_scope() as s:
            return _run(s, as_of)
    return _run(session, as_of)


def _run(session: Session, as_of: _date) -> list[SymbolAnalysis]:
    try:
        symbols = watchlist_symbols(session=session)
    except Exception as exc:
        log.warning("analyze_watchlist: watchlist lookup failed: %s", exc)
        return []
    out: list[SymbolAnalysis] = []
    for sym in symbols:
        try:
            out.append(analyze_symbol(sym, as_of=as_of, session=session))
        except Exception as exc:
            log.warning("analyze_watchlist: %s failed: %s", sym, exc)
            out.append(SymbolAnalysis.unavailable(sym, as_of, f"engine_error: {exc}"))
    return out
