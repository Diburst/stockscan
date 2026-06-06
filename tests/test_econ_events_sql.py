"""Regression tests for the upcoming_events SQL.

Locks the type-cast on the country / importance_min bind params so a
future "simplify the query" refactor can't reintroduce the
``psycopg.errors.AmbiguousParameter: could not determine data type``
error that broke /analysis/{symbol} when the redirect flow re-rendered
the page after Refresh insider.

These tests don't hit Postgres — they inspect the SQL text directly and
exercise the query through a fake session that captures the params.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from stockscan.econ_events.store import _UPCOMING_SQL, upcoming_events


class _FakeResult:
    def all(self) -> list:  # pragma: no cover — trivial
        return []


class _CapturingSession:
    """Records the SQL + params handed to ``execute`` without hitting a DB."""

    def __init__(self) -> None:
        self.executed: list[tuple[Any, dict]] = []

    def execute(self, stmt: Any, params: dict | None = None) -> _FakeResult:
        self.executed.append((stmt, params or {}))
        return _FakeResult()


# ---------------------------------------------------------------------------
# SQL-shape regression tests — the bug we're locking is a *string-level* one,
# so we inspect the rendered SQL directly.
# ---------------------------------------------------------------------------


def test_sql_casts_country_to_text() -> None:
    """The country param must carry an explicit ``::text`` (or CAST AS TEXT)
    so Postgres can prepare the statement."""
    sql_text = str(_UPCOMING_SQL.text).lower()
    # Tolerant of either CAST syntax.
    assert "cast(:country as text)" in sql_text or ":country::text" in sql_text


def test_sql_casts_importance_min_to_text() -> None:
    """Same defensive cast on importance_min — keeps the prepare step type-safe
    even though the literal-string branches usually let Postgres infer."""
    sql_text = str(_UPCOMING_SQL.text).lower()
    assert (
        "cast(:importance_min as text)" in sql_text
        or ":importance_min::text" in sql_text
    )


# ---------------------------------------------------------------------------
# Behavioural tests — ``upcoming_events`` must accept country=None AND a real
# country string without raising on the prepare path. The fake session lets
# us assert it goes through without exception and forwards the right params.
# ---------------------------------------------------------------------------


def _window() -> tuple[datetime, datetime]:
    start = datetime.now(UTC)
    return start, start + timedelta(days=7)


def test_upcoming_events_with_country_none_executes() -> None:
    """country=None used to fail at prepare time on Postgres; must work now."""
    start, end = _window()
    session = _CapturingSession()
    out = upcoming_events(
        start=start, end=end, country=None,
        importance_min="medium", session=session,
    )
    assert out == []
    assert session.executed
    _, params = session.executed[-1]
    assert params["country"] is None
    assert params["importance_min"] == "medium"


def test_upcoming_events_with_country_us_passes_param() -> None:
    """country='US' is the dashboard default — verify it flows through."""
    start, end = _window()
    session = _CapturingSession()
    upcoming_events(
        start=start, end=end, country="US",
        importance_min="high", session=session,
    )
    _, params = session.executed[-1]
    assert params["country"] == "US"
    assert params["importance_min"] == "high"
    assert params["start"] == start
    assert params["end"] == end


@pytest.mark.parametrize("importance_min", ["low", "medium", "high"])
def test_all_importance_min_values_accepted(importance_min: str) -> None:
    """Every supported importance bucket must round-trip through the SQL."""
    start, end = _window()
    session = _CapturingSession()
    upcoming_events(
        start=start, end=end, country="US",
        importance_min=importance_min, session=session,
    )
    _, params = session.executed[-1]
    assert params["importance_min"] == importance_min
