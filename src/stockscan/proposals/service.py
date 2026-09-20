"""The single entry point that assembles a proposed book.

Wires the per-symbol analysis, the market regime, live equity, the sector map
and the macro calendar into the engine and portfolio constructor. MCP, CLI,
the web route and the nightly job all call ``generate_book`` so the pipeline
has one home.
"""

from __future__ import annotations

import logging
from datetime import date as _date
from datetime import datetime, time, timedelta, timezone
from typing import Any, NamedTuple

from sqlalchemy import text
from sqlalchemy.orm import Session

from stockscan.analysis import analyze_watchlist
from stockscan.config import settings
from stockscan.db import session_scope
from stockscan.econ_events import upcoming_events
from stockscan.proposals._models import OptionProposal
from stockscan.proposals.engine import propose_candidates
from stockscan.proposals.portfolio import MAX_BOOK, book_multiplier, build_book
from stockscan.regime import get_regime
from stockscan.sectors.store import sector_map

log = logging.getLogger(__name__)


class ProposalRun(NamedTuple):
    as_of: _date
    regime: Any | None
    candidates: int  # how many cleared triggers/filters before diversification
    book: list[OptionProposal]
    book_mult: float
    macro_events: list[str]  # high-importance US events inside the expiry, "CPI Thu"
    equity: float


def generate_book(
    *,
    list_id: int | None = None,
    n: int = MAX_BOOK,
    as_of: _date | None = None,
    session: Session | None = None,
) -> ProposalRun:
    """Run the full proposal pipeline and return the sized, diversified book.

    Args:
        list_id: Restrict to one watchlist list; None = all watched symbols.
        n: Max book size.
        as_of: Analysis date; default today.
        session: Optional DB session (passed through to every reader).
    """
    as_of = as_of or _date.today()
    analyses = analyze_watchlist(as_of=as_of, list_id=list_id, session=session)
    regime = get_regime(as_of, session=session)
    equity = live_equity(as_of, session=session)
    sectors = sector_map(session=session)
    candidates = propose_candidates(analyses, regime)
    book = build_book(candidates, regime, equity=equity, sectors=sectors, n=n)
    dte = _nearest_dte(analyses)
    macro = macro_events_inside(as_of, dte, session=session) if dte else []
    return ProposalRun(
        as_of=as_of,
        regime=regime,
        candidates=len(candidates),
        book=book,
        book_mult=round(book_multiplier(regime), 3),
        macro_events=macro,
        equity=equity,
    )


def live_equity(as_of: _date, *, session: Session | None = None) -> float:
    """Latest ``equity_history.total_equity`` on or before ``as_of``, else the
    configured starting equity — the same rule the sizer uses."""
    sql = text(
        "SELECT total_equity FROM equity_history WHERE as_of_date <= :d "
        "ORDER BY as_of_date DESC LIMIT 1"
    )

    def _run(s: Session) -> float:
        row = s.execute(sql, {"d": as_of}).first()
        if row is None:
            return float(settings.starting_equity)
        return float(row.total_equity)

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)


def _nearest_dte(analyses: list[Any]) -> int | None:
    """Days to the nearest tenor's expiry — the window the whole book trades."""
    for a in analyses:
        sets = a.options_context.strike_sets if a.available else []
        if sets:
            return sets[0].days_to_expiry
    return None


def macro_events_inside(
    as_of: _date, dte: int, *, session: Session | None = None
) -> list[str]:
    """High-importance US events in ``[as_of, as_of + dte]`` as "CPI Thu"
    strings for the book header. Soft-fails to an empty list."""
    start = datetime.combine(as_of, time.min, tzinfo=timezone.utc)
    end = datetime.combine(as_of + timedelta(days=dte), time.max, tzinfo=timezone.utc)
    try:
        events = upcoming_events(
            start=start, end=end, importance_min="high", session=session
        )
    except Exception as exc:
        log.warning("options: upcoming_events() failed: %s", exc)
        return []
    return [f"{e.event_type} {e.event_ts:%a}" for e in events]
