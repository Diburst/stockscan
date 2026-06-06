"""Persistence + queries for the ``economic_events`` table (migration 0017)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import text

from stockscan.db import session_scope
from stockscan.econ_events.importance import classify_importance

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


@dataclass(frozen=True, slots=True)
class EconomicEvent:
    """One row from ``economic_events``."""

    event_id: int
    event_ts: datetime
    country: str
    event_type: str
    importance: str
    comparison: str | None
    period: str | None
    actual: float | None
    previous: float | None
    estimate: float | None
    change_value: float | None
    change_pct: float | None

    @property
    def is_released(self) -> bool:
        """True once the actual value is in (release has happened)."""
        return self.actual is not None


_UPSERT_SQL = text(
    """
    INSERT INTO economic_events (
        event_ts, country, event_type, comparison, period,
        actual, previous, estimate, change_value, change_pct, importance
    ) VALUES (
        :event_ts, :country, :event_type, :comparison, :period,
        :actual, :previous, :estimate, :change_value, :change_pct, :importance
    )
    ON CONFLICT (event_ts, country, event_type) DO UPDATE SET
        comparison   = EXCLUDED.comparison,
        period       = EXCLUDED.period,
        actual       = EXCLUDED.actual,
        previous     = EXCLUDED.previous,
        estimate     = EXCLUDED.estimate,
        change_value = EXCLUDED.change_value,
        change_pct   = EXCLUDED.change_pct,
        importance   = EXCLUDED.importance,
        fetched_at   = NOW()
    """
)


def upsert_events(
    records: list[dict[str, Any]],
    *,
    session: Session | None = None,
) -> int:
    """Upsert raw EODHD economic-events records. Returns rows touched.

    Records are the provider's JSON shape (date, country, type, actual,
    previous, estimate, comparison, period, change, change_percentage).
    Empty input is a no-op.
    """
    if not records:
        return 0

    payload: list[dict[str, Any]] = []
    for r in records:
        raw_ts = r.get("date")
        if not raw_ts:
            continue
        try:
            # EODHD returns "YYYY-MM-DD HH:MM:SS" (UTC).
            event_ts = datetime.fromisoformat(raw_ts)
        except (ValueError, TypeError):
            continue
        event_type = r.get("type")
        if not event_type:
            continue
        country = r.get("country") or ""
        payload.append({
            "event_ts": event_ts,
            "country": country,
            "event_type": event_type,
            "comparison": r.get("comparison"),
            "period": r.get("period"),
            "actual": r.get("actual"),
            "previous": r.get("previous"),
            "estimate": r.get("estimate"),
            "change_value": r.get("change"),
            "change_pct": r.get("change_percentage"),
            "importance": classify_importance(event_type),
        })

    if not payload:
        return 0

    def _run(s: Session) -> int:
        result = s.execute(_UPSERT_SQL, payload)
        return result.rowcount or len(payload)

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)


_UPCOMING_SQL = text(
    """
    SELECT event_id, event_ts, country, event_type, importance,
           comparison, period, actual, previous, estimate,
           change_value, change_pct
    FROM economic_events
    WHERE event_ts >= :start
      AND event_ts < :end
      -- Postgres can't infer the type of a bare bind param appearing only
      -- on the IS NULL side of an OR, so it raises AmbiguousParameter at
      -- prepare time. The ``::text`` cast pins the type to TEXT for both
      -- sides of the OR. Same trick on importance_min for safety even
      -- though its other branches compare against string literals.
      AND (CAST(:country AS TEXT) IS NULL OR country = :country)
      AND (
            CAST(:importance_min AS TEXT) = 'low'
         OR (CAST(:importance_min AS TEXT) = 'medium' AND importance IN ('medium', 'high'))
         OR (CAST(:importance_min AS TEXT) = 'high'   AND importance = 'high')
      )
    ORDER BY event_ts ASC
    LIMIT :limit
    """
)


def upcoming_events(
    *,
    start: datetime,
    end: datetime,
    country: str | None = "US",
    importance_min: str = "medium",
    limit: int = 200,
    session: Session | None = None,
) -> list[EconomicEvent]:
    """Window query for the dashboard + analysis-detail badge.

    ``importance_min`` is the LOWEST bucket included: ``"high"`` shows
    only top-tier; ``"medium"`` adds the next tier; ``"low"`` shows
    everything. Defaults to ``"medium"`` — the dashboard's typical use.
    """

    def _run(s: Session) -> list[EconomicEvent]:
        rows = s.execute(
            _UPCOMING_SQL,
            {
                "start": start,
                "end": end,
                "country": country,
                "importance_min": importance_min,
                "limit": limit,
            },
        ).all()
        return [
            EconomicEvent(
                event_id=int(r.event_id),
                event_ts=r.event_ts,
                country=r.country,
                event_type=r.event_type,
                importance=r.importance,
                comparison=r.comparison,
                period=r.period,
                actual=float(r.actual) if r.actual is not None else None,
                previous=float(r.previous) if r.previous is not None else None,
                estimate=float(r.estimate) if r.estimate is not None else None,
                change_value=float(r.change_value) if r.change_value is not None else None,
                change_pct=float(r.change_pct) if r.change_pct is not None else None,
            )
            for r in rows
        ]

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)
