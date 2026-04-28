"""Stub provider sanity tests."""

from datetime import date
from decimal import Decimal

from stockscan.data.providers import StubProvider


def test_stub_loads_default_fixture() -> None:
    p = StubProvider()
    bars = p.get_bars("AAPL", date(2024, 1, 1), date(2024, 1, 31))
    assert len(bars) >= 5
    assert all(b.symbol == "AAPL" for b in bars)
    assert all(isinstance(b.open, Decimal) for b in bars)


def test_stub_filters_by_date_range() -> None:
    p = StubProvider()
    a = p.get_bars("AAPL", date(2024, 1, 1), date(2024, 1, 5))
    b = p.get_bars("AAPL", date(2024, 1, 1), date(2024, 1, 31))
    assert len(a) < len(b)


def test_stub_filters_by_symbol() -> None:
    p = StubProvider()
    aapl = p.get_bars("AAPL", date(2024, 1, 1), date(2024, 1, 31))
    msft = p.get_bars("MSFT", date(2024, 1, 1), date(2024, 1, 31))
    assert all(b.symbol == "AAPL" for b in aapl)
    assert all(b.symbol == "MSFT" for b in msft)


def test_stub_constituents_have_valid_shape() -> None:
    p = StubProvider()
    current = p.get_sp500_constituents()
    assert all(m.left_date is None for m in current)
    historical = p.get_sp500_historical_constituents()
    assert len(historical) >= len(current)
