"""Fundamentals payload parsing — pure logic, no DB needed."""

from __future__ import annotations

from stockscan.fundamentals.store import _extract_columns, _to_date, _to_int, _to_number


def test_to_number_handles_strings():
    assert _to_number("3.14") == 3.14
    assert _to_number(3.14) == 3.14
    assert _to_number(3) == 3.0


def test_to_number_filters_invalid():
    assert _to_number(None) is None
    assert _to_number("") is None
    assert _to_number("NA") is None
    assert _to_number("not a number") is None


def test_to_int_uses_to_number():
    assert _to_int("42") == 42
    assert _to_int(None) is None
    assert _to_int("garbage") is None


def test_to_date_parses_iso():
    from datetime import date
    assert _to_date("1976-04-01") == date(1976, 4, 1)
    assert _to_date("1976-04-01T00:00:00Z") == date(1976, 4, 1)
    assert _to_date(None) is None
    assert _to_date("not a date") is None


def test_extract_columns_handles_full_payload():
    payload = {
        "General": {
            "Code": "AAPL",
            "Name": "Apple Inc.",
            "Sector": "Technology",
            "Industry": "Consumer Electronics",
            "CountryName": "USA",
            "CurrencyCode": "USD",
            "Exchange": "NASDAQ",
            "ISIN": "US0378331005",
            "IPODate": "1980-12-12",
        },
        "Highlights": {
            "MarketCapitalization": 3_000_000_000_000,
            "PERatio": 28.5,
            "EarningsShare": 6.42,
            "ProfitMargin": 0.247,
            "RevenueTTM": 391_000_000_000,
            "EBITDA": 130_000_000_000,
            "DividendYield": 0.0052,
        },
        "Valuation": {
            "ForwardPE": 27.1,
            "PriceBookMRQ": 47.2,
            "PriceSalesTTM": 7.8,
        },
        "SharesStats": {
            "SharesOutstanding": 15_500_000_000,
            "SharesFloat": 15_400_000_000,
        },
        "Technicals": {
            "Beta": 1.24,
            "52WeekHigh": 230.50,
            "52WeekLow": 165.10,
            "50DayMA": 220.30,
            "200DayMA": 195.80,
        },
    }
    cols = _extract_columns(payload)
    assert cols["symbol"] == "AAPL"
    assert cols["name"] == "Apple Inc."
    assert cols["sector"] == "Technology"
    assert cols["market_cap"] == 3_000_000_000_000
    assert cols["pe_ratio"] == 28.5
    assert cols["forward_pe"] == 27.1
    assert cols["shares_outstanding"] == 15_500_000_000
    assert cols["beta"] == 1.24
    assert cols["week_52_high"] == 230.5


def test_extract_columns_handles_missing_sections():
    """Sparse payload — most fields missing — should not raise."""
    payload = {"General": {"Code": "X", "Name": "Some Co"}}
    cols = _extract_columns(payload)
    assert cols["symbol"] == "X"
    assert cols["name"] == "Some Co"
    assert cols["market_cap"] is None
    assert cols["pe_ratio"] is None


def test_extract_columns_handles_empty_payload():
    cols = _extract_columns({})
    assert cols["symbol"] is None
    assert cols["market_cap"] is None
    assert cols["raw_payload"] not in cols  # raw_payload is set by upsert, not extract


def test_extract_columns_uses_top_level_code_fallback():
    """Some EODHD responses put Code at the top level, not under General."""
    payload = {"Code": "MSFT", "Highlights": {"MarketCapitalization": 2.5e12}}
    cols = _extract_columns(payload)
    assert cols["symbol"] == "MSFT"
    assert cols["market_cap"] == 2.5e12
