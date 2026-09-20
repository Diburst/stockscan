"""Zero-cost refresh: a refresh with nothing new must make no API calls.

Covers the session-availability gate, the empty-when-current bulk date
list, the watchlist catch-up, and the generic refresh cooldown helper.
"""

from __future__ import annotations

import datetime as dt
from datetime import UTC, date, datetime, timedelta
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from stockscan.data.backfill import EOD_AVAILABLE_HOUR_ET, latest_completed_session
from stockscan.refresh_log import mark_refreshed, refresh_due
from stockscan.data.backfill import catch_up_lagging_symbols, missing_bulk_dates

_NY = ZoneInfo("America/New_York")


def _a_weekday() -> dt.date:
    d = dt.date(2026, 6, 1)
    while d.weekday() >= 5:
        d += dt.timedelta(days=1)
    return d


# --- latest_completed_session ----------------------------------------------
def test_session_evening_is_today():
    wd = _a_weekday()
    evening = datetime(wd.year, wd.month, wd.day, EOD_AVAILABLE_HOUR_ET, 0, tzinfo=_NY)
    assert latest_completed_session(evening) == wd


def test_session_morning_is_prior_trading_day():
    wd = _a_weekday()
    morning = datetime(wd.year, wd.month, wd.day, 9, 30, tzinfo=_NY)
    res = latest_completed_session(morning)
    assert res < wd and res.weekday() < 5


def test_session_weekend_returns_friday():
    wd = _a_weekday()              # Monday 2026-06-01
    friday = wd - dt.timedelta(days=3)
    saturday = wd - dt.timedelta(days=2)
    sunday = wd - dt.timedelta(days=1)
    for day in (saturday, sunday):
        noon = datetime(day.year, day.month, day.day, 12, 0, tzinfo=_NY)
        assert latest_completed_session(noon) == friday


# --- missing_bulk_dates: empty when current ---------------------------------------
def test_bulk_dates_empty_when_current():
    target = latest_completed_session()
    with patch("stockscan.data.store.latest_daily_bar_date", return_value=target):
        assert missing_bulk_dates(7) == []


def test_bulk_dates_nonempty_when_behind():
    target = latest_completed_session()
    with patch(
        "stockscan.data.store.latest_daily_bar_date",
        return_value=target - timedelta(days=10),
    ):
        assert len(missing_bulk_dates(7)) >= 1


# --- watchlist catch-up ----------------------------------------------------
def test_catch_up_fetches_only_the_lagging_watched_name():
    """The market is current but a watched name outside the index fell
    behind (it was dropped by an index-only bulk filter for weeks): the
    catch-up fetches that one symbol from its own last bar, and only it."""
    provider = MagicMock()
    provider.get_bars.return_value = []
    with patch(
        "stockscan.data.backfill.latest_bar_dates",
        return_value={"SPY": date(2026, 9, 18), "AAOI": date(2026, 7, 10)},
    ), patch("stockscan.data.backfill.latest_bar_date", return_value=date(2026, 7, 10)):
        res = catch_up_lagging_symbols(provider, {"SPY", "AAOI"}, target=date(2026, 9, 18))
    assert res.symbols_fetched == ("AAOI",)
    assert provider.get_bars.call_count == 1
    sym, start, _end = provider.get_bars.call_args.args[:3]
    assert sym == "AAOI" and start == date(2026, 7, 5)  # its own last bar minus the overlap


def test_catch_up_makes_no_call_when_every_name_is_current():
    provider = MagicMock()
    with patch("stockscan.data.backfill.latest_bar_dates", return_value={"AAOI": date(2026, 9, 18)}):
        res = catch_up_lagging_symbols(provider, {"AAOI"}, target=date(2026, 9, 18))
    assert res.symbols_fetched == () and res.upserted == 0
    provider.get_bars.assert_not_called()


# --- refresh cooldown helper -----------------------------------------------
class _Row:
    def __init__(self, val):
        self._val = val

    def __getitem__(self, i):
        return self._val


class _CooldownSession:
    """first() returns None (no row) or a 1-col row holding last_success."""

    def __init__(self, last_success):
        self._last = last_success  # datetime, or "NOROW"

    def execute(self, stmt, params=None):
        sql = " ".join(str(stmt).split())
        res = MagicMock()
        if sql.startswith("SELECT last_success"):
            res.first.return_value = None if self._last == "NOROW" else _Row(self._last)
        return res


def test_refresh_due_true_when_never_run():
    assert refresh_due("x", cooldown_hours=20, session=_CooldownSession("NOROW")) is True


def test_refresh_due_true_when_stale():
    old = datetime.now(UTC) - timedelta(hours=30)
    assert refresh_due("x", cooldown_hours=20, session=_CooldownSession(old)) is True


def test_refresh_due_false_when_recent():
    recent = datetime.now(UTC) - timedelta(hours=1)
    assert refresh_due("x", cooldown_hours=20, session=_CooldownSession(recent)) is False


def test_mark_refreshed_executes_upsert():
    sess = MagicMock()
    mark_refreshed("econ_events", session=sess)
    assert sess.execute.called


class _NoTableSession:
    """Simulates an unapplied migration: to_regclass('refresh_log') → NULL."""

    def __init__(self):
        self.insert_attempted = False

    def execute(self, stmt, params=None):
        sql = " ".join(str(stmt).split())
        res = MagicMock()
        if "to_regclass" in sql:
            res.scalar.return_value = None  # table absent
        elif sql.startswith("INSERT"):
            self.insert_attempted = True
        return res


def test_refresh_due_fail_open_when_table_missing():
    # A missing refresh_log must NOT raise — it degrades to "due".
    assert refresh_due("econ_events", cooldown_hours=20, session=_NoTableSession()) is True


def test_mark_refreshed_noop_when_table_missing():
    sess = _NoTableSession()
    mark_refreshed("econ_events", session=sess)  # must not raise
    assert sess.insert_attempted is False  # no INSERT attempted
