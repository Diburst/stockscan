"""Tests for the 23-hour cooldown gate on insider refreshes.

Uses an in-memory ``FakeSession`` that satisfies just enough of the
SQLAlchemy ``Session.execute`` contract for ``can_refresh`` /
``last_successful_refresh`` to query without a real Postgres connection.

What the cooldown MUST guarantee:

  1. First-ever refresh for a scope is always allowed (no log row).
  2. A successful refresh blocks further attempts for ~23 hours.
  3. A FAILED refresh does NOT arm the cooldown — the next attempt
     can proceed.
  4. The cooldown survives "app restart" — since we read from the DB,
     a new session pointing at the same fake row gives the same answer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from stockscan.insider.cooldown import (
    REFRESH_COOLDOWN_HOURS,
    can_refresh,
    last_successful_refresh,
)


@dataclass
class _Row:
    """Stand-in for a SQLAlchemy Row — supports both r[0] and r.attr."""

    completed_at: datetime | None

    def __getitem__(self, idx: int) -> Any:
        if idx == 0:
            return self.completed_at
        raise IndexError(idx)


class _FakeResult:
    """Minimal SQLAlchemy Result-shaped wrapper."""

    def __init__(self, row: _Row | None) -> None:
        self._row = row

    def first(self) -> _Row | None:
        return self._row


class FakeSession:
    """In-memory `Session.execute` stand-in returning a single fixed row."""

    def __init__(self, row: _Row | None = None) -> None:
        self.row = row
        self.calls: list[tuple[Any, dict]] = []

    def execute(self, stmt: Any, params: dict | None = None) -> _FakeResult:
        self.calls.append((stmt, params or {}))
        return _FakeResult(self.row)


def test_no_prior_refresh_allows() -> None:
    """First-ever attempt for a scope: no row → allowed."""
    session = FakeSession(row=None)
    allowed, remaining = can_refresh("watchlist", session=session)
    assert allowed is True
    assert remaining is None


def test_just_refreshed_blocks() -> None:
    """A successful refresh 1 hour ago → blocked, ~22h remaining."""
    one_hour_ago = datetime.now(UTC) - timedelta(hours=1)
    session = FakeSession(row=_Row(completed_at=one_hour_ago))
    allowed, remaining = can_refresh("watchlist", session=session)
    assert allowed is False
    # Allow a few seconds of test execution time.
    assert remaining is not None
    expected_remaining = REFRESH_COOLDOWN_HOURS * 3600 - 3600  # ~22h in seconds
    assert abs(remaining - expected_remaining) < 30


def test_cooldown_elapsed_allows_again() -> None:
    """A successful refresh 24 hours ago → allowed (cooldown is 23h)."""
    long_ago = datetime.now(UTC) - timedelta(hours=24)
    session = FakeSession(row=_Row(completed_at=long_ago))
    allowed, remaining = can_refresh("watchlist", session=session)
    assert allowed is True
    assert remaining is None


def test_naive_timestamp_is_treated_as_utc() -> None:
    """Defensive: rows without tzinfo are coerced to UTC, not crashed on."""
    naive = (datetime.now(UTC) - timedelta(hours=2)).replace(tzinfo=None)
    session = FakeSession(row=_Row(completed_at=naive))
    allowed, _ = can_refresh("watchlist", session=session)
    assert allowed is False  # 2h ago → still within cooldown


def test_cooldown_hours_override() -> None:
    """The cooldown_hours kwarg overrides REFRESH_COOLDOWN_HOURS for testing."""
    # Last refresh 30 minutes ago.
    half_hour_ago = datetime.now(UTC) - timedelta(minutes=30)
    session = FakeSession(row=_Row(completed_at=half_hour_ago))
    # With a 0.25h (15-minute) cooldown, half an hour is enough to allow.
    allowed, _ = can_refresh("watchlist", cooldown_hours=0.25, session=session)
    assert allowed is True
    # With a 1h cooldown, 30 minutes is not enough.
    allowed, remaining = can_refresh("watchlist", cooldown_hours=1.0, session=session)
    assert allowed is False
    assert remaining is not None and 0 < remaining <= 1800  # somewhere in (0, 30 min]


def test_last_successful_refresh_returns_row_value() -> None:
    when = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
    session = FakeSession(row=_Row(completed_at=when))
    got = last_successful_refresh("watchlist", session=session)
    assert got == when


def test_last_successful_refresh_returns_none_for_empty() -> None:
    session = FakeSession(row=None)
    assert last_successful_refresh("watchlist", session=session) is None


@pytest.mark.parametrize("scope", ["watchlist", "symbol:AAPL", "symbol:MSFT"])
def test_per_scope_isolation_via_query_params(scope: str) -> None:
    """The scope string flows through to the SQL params — used to enforce
    that the watchlist refresh and per-symbol refreshes don't interfere
    with each other's cooldowns."""
    session = FakeSession(row=None)
    can_refresh(scope, session=session)
    # The SELECT query was called with the matching scope param.
    assert session.calls[-1][1] == {"scope": scope}
