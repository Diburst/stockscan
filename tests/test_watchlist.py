"""Watchlist alert logic — tests target satisfaction + formatting + symbol validation.

DB-backed tests for the store module are integration tests; we exercise the
pure-logic paths here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest

from stockscan.watchlist.alerts import _format_body, _format_subject, check_and_fire_alerts
from stockscan.watchlist.store import WatchlistItem, _normalize_symbol


def _item(
    *,
    target: Decimal | None = None,
    direction: str | None = None,
    last_close: Decimal | None = None,
    prev_close: Decimal | None = None,
    alert_enabled: bool = True,
) -> WatchlistItem:
    return WatchlistItem(
        watchlist_id=1,
        symbol="AAPL",
        target_price=target,
        target_direction=direction,  # type: ignore[arg-type]
        alert_enabled=alert_enabled,
        last_alerted_at=None,
        last_triggered_price=None,
        note=None,
        created_at=datetime(2026, 4, 27, tzinfo=timezone.utc),
        last_close=last_close,
        prev_close=prev_close,
        last_volume=1_000_000,
        last_bar_date=datetime(2026, 4, 27, 16, tzinfo=timezone.utc),
    )


# --- Symbol validation ---
def test_normalize_uppercases():
    assert _normalize_symbol("aapl") == "AAPL"
    assert _normalize_symbol("  msft  ") == "MSFT"


def test_normalize_rejects_invalid():
    # NB: lowercase is valid input (it gets uppercased — see test_normalize_uppercases),
    # so it must NOT be in this reject list. Use a space-containing string instead.
    for bad in ["", "1AAPL", "@@", "a" * 11, "AB CD"]:
        with pytest.raises(ValueError):
            _normalize_symbol(bad)


def test_normalize_allows_punctuation():
    # Tickers like BRK.B are valid
    assert _normalize_symbol("BRK.B") == "BRK.B"
    assert _normalize_symbol("BF-B") == "BF-B"


# --- target_satisfied logic ---
def test_target_above_satisfied_when_close_at_or_above():
    it = _item(target=Decimal("200"), direction="above", last_close=Decimal("200"))
    assert it.target_satisfied is True
    it = _item(target=Decimal("200"), direction="above", last_close=Decimal("201"))
    assert it.target_satisfied is True


def test_target_above_not_satisfied_below():
    it = _item(target=Decimal("200"), direction="above", last_close=Decimal("199.99"))
    assert it.target_satisfied is False


def test_target_below_satisfied_when_close_at_or_below():
    it = _item(target=Decimal("100"), direction="below", last_close=Decimal("100"))
    assert it.target_satisfied is True
    it = _item(target=Decimal("100"), direction="below", last_close=Decimal("95"))
    assert it.target_satisfied is True


def test_target_below_not_satisfied_above():
    it = _item(target=Decimal("100"), direction="below", last_close=Decimal("100.01"))
    assert it.target_satisfied is False


def test_no_target_never_satisfied():
    it = _item(last_close=Decimal("999"))
    assert it.target_satisfied is False


def test_no_close_never_satisfied():
    it = _item(target=Decimal("200"), direction="above")
    assert it.target_satisfied is False


# --- pct_change_today ---
def test_pct_change_handles_normal_case():
    it = _item(last_close=Decimal("110"), prev_close=Decimal("100"))
    assert it.pct_change_today == pytest.approx(0.10)


def test_pct_change_returns_none_when_missing():
    assert _item(last_close=Decimal("100")).pct_change_today is None
    assert _item(prev_close=Decimal("100")).pct_change_today is None


# --- Formatting ---
def test_subject_above():
    it = _item(target=Decimal("200.00"), direction="above", last_close=Decimal("201.50"))
    assert _format_subject(it) == "AAPL crossed above $200.00"


def test_subject_below():
    it = _item(target=Decimal("100.00"), direction="below", last_close=Decimal("95.20"))
    assert _format_subject(it) == "AAPL crossed below $100.00"


def test_body_includes_pct_change():
    it = _item(
        target=Decimal("200"), direction="above",
        last_close=Decimal("210"), prev_close=Decimal("200"),
    )
    body = _format_body(it)
    assert "AAPL" in body
    assert "above target" in body
    assert "$210" in body
    assert "+5.00%" in body


# --- Orchestration ---
def test_check_and_fire_alerts_fires_only_triggered():
    triggered = _item(target=Decimal("200"), direction="above", last_close=Decimal("210"))
    not_triggered = _item(target=Decimal("200"), direction="above", last_close=Decimal("190"))

    captured = []

    def _fake_notify(subject, body, *, priority="normal", channels=None):
        captured.append((subject, priority))
        return 1

    with patch("stockscan.watchlist.alerts.get_triggered", return_value=[triggered]), \
         patch("stockscan.watchlist.alerts.notify", side_effect=_fake_notify), \
         patch("stockscan.watchlist.alerts.mark_alerted") as mark:
        result = check_and_fire_alerts()

    assert len(result.fired) == 1
    assert result.fired[0].symbol == "AAPL"
    assert captured == [("AAPL crossed above $200.00", "high")]
    mark.assert_called_once()


def test_check_and_fire_alerts_with_no_triggers():
    with patch("stockscan.watchlist.alerts.get_triggered", return_value=[]):
        result = check_and_fire_alerts()
    assert result.fired == []


# --- Multi-list selection logic (resolve_selection) ------------------------
# These exercise the pure mapping from a ``?list=`` query value to a
# (list_id|None, label) selection without a real DB, via a tiny fake session
# that answers the two SQL statements resolve_selection issues.
from stockscan.watchlist.store import (  # noqa: E402
    DEFAULT_LIST_NAME,
    resolve_selection,
)


class _Row:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Result:
    def __init__(self, rows=None, scalar=None):
        self._rows = rows or []
        self._scalar = scalar

    def __iter__(self):
        return iter(self._rows)

    def all(self):
        return self._rows

    def scalar_one(self):
        return self._scalar

    def scalar(self):
        return self._scalar


class _FakeSession:
    """Answers the SELECT (list_watchlists) and INSERT (create_watchlist)
    that resolve_selection runs. New lists get an auto-incrementing id."""

    def __init__(self, lists):
        # lists: list of (list_id, name, count)
        self.lists = list(lists)
        self._next_id = 1000

    def execute(self, stmt, params=None):
        sql = " ".join(str(stmt).split())
        if "FROM watchlists wl" in sql:
            return _Result(
                rows=[_Row(list_id=i, name=n, cnt=c) for (i, n, c) in self.lists]
            )
        if "INSERT INTO watchlists" in sql:
            nid = self._next_id
            self._next_id += 1
            self.lists.append((nid, params["nm"], 0))
            return _Result(scalar=nid)
        return _Result()


def test_resolve_selection_all_maps_to_none():
    sess = _FakeSession([(1, "Watchlist", 3)])
    assert resolve_selection("all", session=sess) == (None, "All")
    assert resolve_selection("ALL", session=sess) == (None, "All")


def test_resolve_selection_known_id():
    sess = _FakeSession([(1, "Watchlist", 3), (2, "Tech", 5)])
    assert resolve_selection("2", session=sess) == (2, "Tech")


def test_resolve_selection_blank_falls_back_to_default():
    sess = _FakeSession([(1, "Watchlist", 3), (2, "Tech", 5)])
    assert resolve_selection(None, session=sess) == (1, DEFAULT_LIST_NAME)


def test_resolve_selection_unknown_id_falls_back_to_default():
    sess = _FakeSession([(1, "Watchlist", 3)])
    assert resolve_selection("999", session=sess) == (1, DEFAULT_LIST_NAME)


def test_resolve_selection_creates_default_when_missing():
    # Fresh DB with no lists at all → default list is created on the fly.
    sess = _FakeSession([])
    list_id, label = resolve_selection(None, session=sess)
    assert label == DEFAULT_LIST_NAME
    assert list_id == 1000  # the id the fake session minted for the INSERT


# --- Bulk add (add_symbols) + rename (rename_watchlist) --------------------
from stockscan.watchlist.store import (  # noqa: E402
    add_symbols,
    rename_watchlist,
)


class _BulkSession:
    """Answers the item + membership inserts add_symbols issues. Tracks the
    memberships created so tests can assert what landed on which list."""

    def __init__(self):
        self._next_wid = 1
        self.items = {}          # symbol -> wid
        self.memberships = []    # (list_id, wid)

    def execute(self, stmt, params=None):
        sql = " ".join(str(stmt).split())
        if "INSERT INTO watchlist_items" in sql:
            sym = params["sym"]
            wid = self.items.get(sym)
            if wid is None:
                wid = self._next_wid
                self._next_wid += 1
                self.items[sym] = wid
            return _Result(scalar=wid)
        if "INSERT INTO watchlist_membership" in sql:
            self.memberships.append((params["lid"], params["wid"]))
            return _Result()
        if "INSERT INTO watchlists" in sql:  # create_watchlist (new_list_name)
            wid = self._next_wid
            self._next_wid += 1
            return _Result(scalar=wid)
        return _Result()


def test_add_symbols_parses_mixed_separators():
    sess = _BulkSession()
    res = add_symbols("AAPL, msft  GOOG;tsla\nbrk.b", list_id=5, session=sess)
    assert res.added == ["AAPL", "MSFT", "GOOG", "TSLA", "BRK.B"]
    assert res.invalid == []
    assert res.list_id == 5
    # Every symbol got a membership row on list 5.
    assert {lid for (lid, _) in sess.memberships} == {5}
    assert len(sess.memberships) == 5


def test_add_symbols_collects_invalid_and_dedupes():
    sess = _BulkSession()
    res = add_symbols("AAPL, 123, @@, AAPL, aapl, MSFT", list_id=1, session=sess)
    assert res.added == ["AAPL", "MSFT"]      # deduped, normalized
    assert res.invalid == ["123", "@@"]


def test_add_symbols_empty_input():
    sess = _BulkSession()
    res = add_symbols("   ", list_id=1, session=sess)
    assert res.added == [] and res.invalid == []


def test_add_symbols_new_list_name_creates_list():
    sess = _BulkSession()
    res = add_symbols("AAPL", new_list_name="Movers", session=sess)
    # create_watchlist minted id 1 for the new list; AAPL is wid 2.
    assert res.list_id == 1
    assert res.added == ["AAPL"]


class _RenameSession:
    def __init__(self, existing=None):
        self.existing = existing or {}   # name -> list_id
        self.updated = None

    def execute(self, stmt, params=None):
        sql = " ".join(str(stmt).split())
        if sql.startswith("SELECT list_id FROM watchlists WHERE name"):
            return _Result(scalar=self.existing.get(params["nm"]))
        if "UPDATE watchlists SET name" in sql:
            self.updated = (params["lid"], params["nm"])
            return _Result()
        return _Result()


def test_rename_watchlist_ok():
    sess = _RenameSession()
    rename_watchlist(2, "Long Term", session=sess)
    assert sess.updated == (2, "Long Term")


def test_rename_watchlist_same_list_keeping_name_ok():
    # The name resolves to the SAME list being renamed → not a collision.
    sess = _RenameSession(existing={"Tech": 9})
    rename_watchlist(9, "Tech", session=sess)
    assert sess.updated == (9, "Tech")


def test_rename_watchlist_collision_raises():
    sess = _RenameSession(existing={"Tech": 9})
    with pytest.raises(ValueError):
        rename_watchlist(2, "Tech", session=sess)


def test_rename_watchlist_empty_name_raises():
    with pytest.raises(ValueError):
        rename_watchlist(2, "   ", session=_RenameSession())
