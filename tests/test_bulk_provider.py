"""Bulk EOD endpoint: stub provider behaviour + ABC default."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from stockscan.data.providers import StubProvider
from stockscan.data.providers.base import BarRow
from zoneinfo import ZoneInfo
from datetime import datetime, timezone

NY_TZ = ZoneInfo("America/New_York")


def _make_bar(symbol: str, d: date, close: float = 100.0) -> BarRow:
    ts = datetime(d.year, d.month, d.day, 16, tzinfo=NY_TZ).astimezone(timezone.utc)
    return BarRow(
        symbol=symbol, bar_ts=ts, interval="1d",
        open=Decimal(str(close)), high=Decimal(str(close + 1)),
        low=Decimal(str(close - 1)), close=Decimal(str(close)),
        adj_close=Decimal(str(close)), volume=1_000_000, source="stub",
    )


def test_stub_bulk_returns_all_for_date():
    bars = [
        _make_bar("AAPL", date(2024, 1, 2)),
        _make_bar("MSFT", date(2024, 1, 2)),
        _make_bar("AAPL", date(2024, 1, 3)),
    ]
    p = StubProvider(bars=bars)
    out = p.get_eod_bulk(date(2024, 1, 2))
    syms = {b.symbol for b in out}
    assert syms == {"AAPL", "MSFT"}


def test_stub_bulk_filters_by_symbols():
    bars = [
        _make_bar("AAPL", date(2024, 1, 2)),
        _make_bar("MSFT", date(2024, 1, 2)),
        _make_bar("GOOG", date(2024, 1, 2)),
    ]
    p = StubProvider(bars=bars)
    out = p.get_eod_bulk(date(2024, 1, 2), symbols=["AAPL", "GOOG"])
    syms = {b.symbol for b in out}
    assert syms == {"AAPL", "GOOG"}


def test_stub_bulk_empty_for_unknown_date():
    bars = [_make_bar("AAPL", date(2024, 1, 2))]
    p = StubProvider(bars=bars)
    out = p.get_eod_bulk(date(2099, 1, 1))
    assert out == []


def test_trading_days_since_helper():
    from stockscan.data.backfill import trading_days_since
    # Mon Jan 1 2024, going to Mon Jan 8 2024
    days = trading_days_since(date(2024, 1, 1), date(2024, 1, 8))
    # Expect Tue 2, Wed 3, Thu 4, Fri 5, Mon 8 (skip weekend)
    assert len(days) == 5
    assert days[0] == date(2024, 1, 2)
    assert days[-1] == date(2024, 1, 8)
    weekdays = {d.weekday() for d in days}
    assert weekdays.issubset({0, 1, 2, 3, 4})


def test_trading_days_since_none_uses_recent_window():
    from stockscan.data.backfill import trading_days_since
    days = trading_days_since(None, date(2024, 1, 8))
    # Should be 1-2 trading days max (10 calendar day fallback − weekends)
    assert len(days) >= 1
