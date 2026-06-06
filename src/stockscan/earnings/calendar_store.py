"""Persistence + queries for the ``earnings_calendar`` table.

The table was created in migration 0001 but never had an upsert path
wired through the codebase — ``compute_options_context`` reads from it
but until this package shipped, nothing was writing rows. This module
provides the missing upsert layer + the query helpers the analysis
page needs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as _date
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import text

from stockscan.db import session_scope

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from stockscan.data.providers.base import EarningsRow


@dataclass(frozen=True, slots=True)
class EarningsEntry:
    """One row from ``earnings_calendar``."""

    symbol: str
    report_date: _date
    time_of_day: str  # 'bmo' | 'amc' | 'unknown'
    estimate: Decimal | None
    actual: Decimal | None

    @property
    def display_when(self) -> str:
        """Human-readable timing pill: 'BMO', 'AMC', or '—'."""
        return {"bmo": "BMO", "amc": "AMC"}.get(self.time_of_day, "—")


_UPSERT_SQL = text(
    """
    INSERT INTO earnings_calendar (symbol, report_date, time_of_day, estimate, actual)
    VALUES (:symbol, :report_date, :time_of_day, :estimate, :actual)
    ON CONFLICT (symbol, report_date) DO UPDATE SET
        time_of_day = EXCLUDED.time_of_day,
        estimate    = COALESCE(EXCLUDED.estimate, earnings_calendar.estimate),
        actual      = COALESCE(EXCLUDED.actual,   earnings_calendar.actual),
        fetched_at  = NOW()
    """
)


def upsert_earnings(
    rows: list[EarningsRow],
    *,
    session: Session | None = None,
) -> int:
    """Upsert EarningsRow records into ``earnings_calendar``.

    Returns rows touched. The COALESCE on estimate/actual is intentional:
    a re-pull of an already-reported earnings event should not blank
    out the actual if EODHD's payload happens to omit it.
    """
    if not rows:
        return 0
    payload = [
        {
            "symbol": r.symbol,
            "report_date": r.report_date,
            "time_of_day": r.time_of_day,
            "estimate": r.estimate,
            "actual": r.actual,
        }
        for r in rows
    ]

    def _run(s: Session) -> int:
        result = s.execute(_UPSERT_SQL, payload)
        return result.rowcount or len(payload)

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)


_NEXT_SQL = text(
    """
    SELECT symbol, report_date, time_of_day, estimate, actual
    FROM earnings_calendar
    WHERE symbol = :symbol AND report_date >= :as_of
    ORDER BY report_date ASC
    LIMIT 1
    """
)


def next_earnings(
    symbol: str,
    *,
    as_of: _date | None = None,
    session: Session | None = None,
) -> EarningsEntry | None:
    """The soonest upcoming (or today's) earnings for ``symbol``."""
    if as_of is None:
        as_of = _date.today()

    def _run(s: Session) -> EarningsEntry | None:
        row = s.execute(_NEXT_SQL, {"symbol": symbol, "as_of": as_of}).first()
        if row is None:
            return None
        return EarningsEntry(
            symbol=row.symbol,
            report_date=row.report_date,
            time_of_day=row.time_of_day or "unknown",
            estimate=row.estimate,
            actual=row.actual,
        )

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)


_THIS_WEEK_SQL = text(
    """
    SELECT symbol, report_date, time_of_day, estimate, actual
    FROM earnings_calendar
    WHERE report_date BETWEEN :start AND :end
      AND symbol = ANY(:symbols)
    ORDER BY report_date ASC, symbol ASC
    """
)


def earnings_in_window(
    symbols: list[str],
    *,
    start: _date,
    end: _date,
    session: Session | None = None,
) -> list[EarningsEntry]:
    """All earnings events for ``symbols`` in the date window.

    Used by the dashboard's "Earnings this week" card — pass the
    watchlist symbols + a 5-day window.
    """
    if not symbols:
        return []

    def _run(s: Session) -> list[EarningsEntry]:
        rows = s.execute(
            _THIS_WEEK_SQL,
            {"symbols": list(symbols), "start": start, "end": end},
        ).all()
        return [
            EarningsEntry(
                symbol=r.symbol,
                report_date=r.report_date,
                time_of_day=r.time_of_day or "unknown",
                estimate=r.estimate,
                actual=r.actual,
            )
            for r in rows
        ]

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)


def days_until(report_date: _date, as_of: _date | None = None) -> int:
    """Calendar days from ``as_of`` (default today) to ``report_date``."""
    if as_of is None:
        as_of = _date.today()
    return (report_date - as_of).days
