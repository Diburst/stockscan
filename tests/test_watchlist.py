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
    for bad in ["", "1AAPL", "@@", "a" * 11, "lower"]:
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
