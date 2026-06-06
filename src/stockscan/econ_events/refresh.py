"""Fetch + persist EODHD economic events.

One API call per page. Default window: prior 7 days through next 30 days,
so the dashboard sees fresh upcoming releases AND we keep the historical
actual / estimate prints for post-release surprise context (visible on
the analysis-detail badge after a release lands).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from stockscan.econ_events.store import upsert_events

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from stockscan.data.providers.base import DataProvider

log = logging.getLogger(__name__)


# Default window: 7 days back (catches recent prints) + 30 days forward
# (covers the dashboard's "this week / next two weeks" surface).
_DEFAULT_DAYS_BACK = 7
_DEFAULT_DAYS_FORWARD = 30


@dataclass(frozen=True, slots=True)
class EconEventsRefreshResult:
    upserted: int
    started_at: datetime
    finished_at: datetime
    error: str | None = None

    @property
    def duration_seconds(self) -> float:
        return (self.finished_at - self.started_at).total_seconds()


def refresh_economic_events(
    provider: DataProvider,
    *,
    country: str | None = "US",
    days_back: int = _DEFAULT_DAYS_BACK,
    days_forward: int = _DEFAULT_DAYS_FORWARD,
    session: Session | None = None,
) -> EconEventsRefreshResult:
    """Pull economic events from the provider and upsert into the local store.

    Soft-fails: a provider error returns a result with ``error`` set rather
    than raising, so the watchlist refresh route can surface "X bars
    refreshed but the macro calendar failed" without blanking the whole
    user flow.
    """
    started = datetime.now(UTC)
    today = datetime.now(UTC).date()
    start = today - timedelta(days=days_back)
    end = today + timedelta(days=days_forward)

    error: str | None = None
    upserted = 0
    try:
        # The provider returns the raw JSON list; the store layer assigns
        # importance and dedupes by natural key.
        if not hasattr(provider, "get_economic_events"):
            error = "provider does not implement get_economic_events"
        else:
            records = provider.get_economic_events(  # type: ignore[attr-defined]
                country=country, start=start, end=end, limit=1000,
            )
            upserted = upsert_events(records, session=session)
    except Exception as exc:  # broad — provider may raise httpx errors
        log.warning("refresh_economic_events: provider call failed: %s", exc)
        error = str(exc)

    finished = datetime.now(UTC)
    return EconEventsRefreshResult(
        upserted=upserted,
        started_at=started,
        finished_at=finished,
        error=error,
    )
