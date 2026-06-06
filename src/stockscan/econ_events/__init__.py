"""Economic events calendar — CPI / NFP / FOMC / ISM / etc.

Backed by ``economic_events`` (migration 0017). Dashboard renders the
next 5-7 days; analysis-detail surfaces high-importance events as a
small badge so position-entry decisions account for known vol catalysts.

Pulls from EODHD ``/api/economic-events`` (1 API call per page) — cheap
enough to refresh on every "Refresh bars" click without budget concern.
"""

from __future__ import annotations

from stockscan.econ_events.importance import classify_importance
from stockscan.econ_events.refresh import refresh_economic_events
from stockscan.econ_events.store import (
    EconomicEvent,
    upcoming_events,
    upsert_events,
)

__all__ = [
    "EconomicEvent",
    "classify_importance",
    "refresh_economic_events",
    "upcoming_events",
    "upsert_events",
]
