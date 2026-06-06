"""Regression tests for the earnings_trends query shape + revision_summary
tiebreak.

What we're locking:

  * The SELECT carries a ``since`` filter and casts it to DATE (same
    AmbiguousParameter trick we used on the econ-events query).
  * ORDER BY is ``period_end DESC`` so the most forward-looking trend
    row renders first on the analysis page.
  * ``revision_summary`` picks the LATEST ``period_end`` within the
    ``+1q`` bucket — so when EODHD republishes historical ``+1q``
    snapshots whose quarters have since closed, the watchlist column
    still shows the current actionable estimate.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from stockscan.earnings.trends_store import (
    _SELECT_SQL,
    EarningsTrend,
    latest_trend,
    revision_summary,
)


# ---------------------------------------------------------------------------
# SQL-shape tests
# ---------------------------------------------------------------------------


def test_sql_has_since_filter() -> None:
    sql_text = str(_SELECT_SQL.text).lower()
    assert "period_end >= :since" in sql_text


def test_sql_casts_since_to_date() -> None:
    """:since needs the CAST so Postgres can prepare the statement even
    when the caller passes None."""
    sql_text = str(_SELECT_SQL.text).lower()
    assert "cast(:since as date)" in sql_text or ":since::date" in sql_text


def test_sql_orders_period_end_descending() -> None:
    sql_text = str(_SELECT_SQL.text).lower()
    assert "order by period_end desc" in sql_text


# ---------------------------------------------------------------------------
# Behavioural tests through a fake session
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class _FakeSession:
    """Returns whatever rows you stuff into it on the next ``execute``."""

    def __init__(self, rows: list[Any] | None = None) -> None:
        self.rows = rows or []
        self.executed: list[tuple[Any, dict]] = []

    def execute(self, stmt: Any, params: dict | None = None) -> _FakeResult:
        self.executed.append((stmt, params or {}))
        return _FakeResult(self.rows)


def test_latest_trend_forwards_since_param() -> None:
    """``since=`` flows through as a SQL bind param so the WHERE
    clause sees the right cutoff."""
    cutoff = date(2025, 1, 1)
    session = _FakeSession()
    latest_trend("AAPL", since=cutoff, session=session)
    _, params = session.executed[-1]
    assert params == {"symbol": "AAPL", "since": cutoff}


def test_latest_trend_defaults_since_to_none() -> None:
    """Default since is None — i.e., backward-compat: return everything."""
    session = _FakeSession()
    latest_trend("AAPL", session=session)
    _, params = session.executed[-1]
    assert params["since"] is None


# ---------------------------------------------------------------------------
# revision_summary tiebreak tests (no SQL, pure Python ordering)
# ---------------------------------------------------------------------------


def _trend(period: str, period_end: date, **overrides) -> EarningsTrend:
    defaults: dict[str, Any] = {
        "trend_id": id(period_end),
        "symbol": "AAPL",
        "period_end": period_end,
        "period": period,
        "eps_estimate_avg": None,
        "eps_estimate_low": None,
        "eps_estimate_high": None,
        "eps_year_ago": None,
        "eps_growth": None,
        "eps_analyst_count": None,
        "rev_estimate_avg": None,
        "rev_estimate_low": None,
        "rev_estimate_high": None,
        "rev_year_ago": None,
        "rev_growth": None,
        "rev_analyst_count": None,
        "eps_trend_current": None,
        "eps_trend_7d_ago": None,
        "eps_trend_30d_ago": None,
        "eps_trend_60d_ago": None,
        "eps_trend_90d_ago": None,
        "eps_revisions_up_7d": None,
        "eps_revisions_up_30d": None,
        "eps_revisions_down_30d": None,
    }
    defaults.update(overrides)
    return EarningsTrend(**defaults)


def test_revision_summary_picks_plus_1q_over_other_periods() -> None:
    """+1q wins regardless of period_end — it's the highest-priority bucket."""
    session = _FakeSession(rows=[
        _trend("0q", date(2025, 12, 31)),
        _trend("+1q", date(2026, 3, 31)),
        _trend("0y", date(2025, 12, 31)),
        _trend("+1y", date(2027, 3, 31)),
    ])
    summary = revision_summary("AAPL", session=session)
    assert summary is not None
    assert summary.period == "+1q"


def test_revision_summary_picks_latest_period_end_within_same_bucket() -> None:
    """When EODHD returns multiple +1q snapshots (one current, one historical),
    we MUST pick the latest period_end — the current forward-looking row.

    This is the bug the tiebreak fix addresses."""
    today = date(2026, 6, 1)
    stale = _trend("+1q", today - timedelta(days=300))  # quarter long-ended
    current = _trend("+1q", today + timedelta(days=120))  # actual next quarter
    session = _FakeSession(rows=[stale, current])
    summary = revision_summary("AAPL", session=session)
    assert summary is not None
    assert summary.period_end == current.period_end


def test_revision_summary_priority_order_matches_actionability() -> None:
    """Order: +1q > 0q > +1y > 0y — matches the actionability for
    swing-horizon trading (next quarter most actionable, current fiscal
    year least urgent of the four)."""
    session = _FakeSession(rows=[
        _trend("0y", date(2025, 12, 31)),  # lowest priority
        _trend("+1y", date(2026, 12, 31)),
        _trend("0q", date(2025, 12, 31)),
        _trend("+1q", date(2026, 3, 31)),  # highest priority
    ])
    assert revision_summary("AAPL", session=session).period == "+1q"

    session2 = _FakeSession(rows=[
        _trend("0y", date(2025, 12, 31)),
        _trend("+1y", date(2026, 12, 31)),
        _trend("0q", date(2025, 12, 31)),
    ])
    assert revision_summary("AAPL", session=session2).period == "0q"


def test_revision_summary_returns_none_for_empty() -> None:
    session = _FakeSession(rows=[])
    assert revision_summary("AAPL", session=session) is None
