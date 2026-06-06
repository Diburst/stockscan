"""Fetch + persist earnings calendar + estimate trends.

Two cheap (1 API call) endpoints, both populated for the watchlist
symbols on each "Refresh bars" click:

  * ``refresh_earnings_calendar`` — pulls /api/calendar/earnings for a
    symbol list over the next ~90 days. Populates ``earnings_calendar``
    so the analysis page's days-to-earnings and the dashboard's
    "Earnings this week" card both have current data.

  * ``refresh_earnings_trends`` — pulls /api/calendar/trends for a
    symbol list. One call per batch of 100 symbols; upserts into
    ``earnings_trends``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

from stockscan.earnings.calendar_store import upsert_earnings
from stockscan.earnings.trends_store import upsert_trends

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from stockscan.data.providers.base import DataProvider

log = logging.getLogger(__name__)


# Earnings calendar window: 90 days forward covers the next reporting
# season + the buffer for IR-announced confirmations.
_DEFAULT_DAYS_FORWARD = 90


@dataclass(frozen=True, slots=True)
class EarningsRefreshResult:
    calendar_upserted: int
    trends_upserted: int
    started_at: datetime
    finished_at: datetime
    error: str | None = None

    @property
    def duration_seconds(self) -> float:
        return (self.finished_at - self.started_at).total_seconds()


def refresh_earnings_calendar(
    provider: DataProvider,
    symbols: list[str],
    *,
    days_forward: int = _DEFAULT_DAYS_FORWARD,
    session: Session | None = None,
) -> int:
    """Pull upcoming earnings for ``symbols`` and upsert. Returns rows touched."""
    if not symbols:
        return 0
    today = date.today()
    end = today + timedelta(days=days_forward)
    try:
        rows = provider.get_earnings(symbols, today, end)
    except Exception as exc:
        log.warning("refresh_earnings_calendar: provider call failed: %s", exc)
        return 0
    return upsert_earnings(rows, session=session)


def refresh_earnings_trends(
    provider: DataProvider,
    symbols: list[str],
    *,
    session: Session | None = None,
) -> int:
    """Pull /calendar/trends for ``symbols`` and upsert. Returns rows touched."""
    if not symbols:
        return 0
    if not hasattr(provider, "get_calendar_trends"):
        log.warning(
            "refresh_earnings_trends: provider %r does not implement "
            "get_calendar_trends",
            type(provider).__name__,
        )
        return 0
    try:
        grouped = provider.get_calendar_trends(symbols)  # type: ignore[attr-defined]
    except Exception as exc:
        log.warning("refresh_earnings_trends: provider call failed: %s", exc)
        return 0
    return upsert_trends(grouped, session=session)


def refresh_earnings(
    provider: DataProvider,
    symbols: list[str],
    *,
    days_forward: int = _DEFAULT_DAYS_FORWARD,
    session: Session | None = None,
) -> EarningsRefreshResult:
    """Run both ``refresh_earnings_calendar`` + ``refresh_earnings_trends``
    for ``symbols``. Aggregates per-call counts and any error."""
    started = datetime.now(UTC)
    error: str | None = None
    try:
        cal = refresh_earnings_calendar(
            provider, symbols, days_forward=days_forward, session=session,
        )
    except Exception as exc:  # safety net — refresh_* swallow most errors
        log.warning("refresh_earnings: calendar leg failed: %s", exc)
        cal = 0
        error = f"calendar: {exc}"
    try:
        trends = refresh_earnings_trends(provider, symbols, session=session)
    except Exception as exc:
        log.warning("refresh_earnings: trends leg failed: %s", exc)
        trends = 0
        error = (error + "; " if error else "") + f"trends: {exc}"
    finished = datetime.now(UTC)
    return EarningsRefreshResult(
        calendar_upserted=cal,
        trends_upserted=trends,
        started_at=started,
        finished_at=finished,
        error=error,
    )
