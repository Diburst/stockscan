"""Tests for fundamentals numeric coercion (regression for SLG payout_ratio).

SLG returned ``payout_ratio = 153.75`` into a ``NUMERIC(8,6)`` column (cap
< 100), which raised psycopg ``NumericValueOutOfRange`` and aborted the whole
row. Migration 0014 widens the column to ``NUMERIC(12,6)`` and
``_fit_numeric`` coerces anything still out of range (or non-finite) to NULL.
"""

from __future__ import annotations

import math

import pytest

from stockscan.fundamentals.store import _extract_columns, _fit_int, _fit_numeric


# Minimal payload mirroring the EODHD shape that triggered the bug.
SLG_PAYLOAD = {
    "General": {
        "Code": "SLG",
        "Name": "SL Green Realty Corp",
        "Sector": "Real Estate",
        "Industry": "REIT - Office",
        "CountryName": "USA",
        "CurrencyCode": "USD",
        "Exchange": "NYSE",
        "ISIN": "US78440X8873",
        "IPODate": "1997-08-15",
    },
    "Highlights": {
        "MarketCapitalization": 3331911168.0,
        "PERatio": None,
        "PEGRatio": 1.3002,
        "EarningsShare": -2.51,
        "EPSEstimateCurrentYear": -6.3108,
        "BookValue": 46.6,
        "ProfitMargin": -0.1617,
        "OperatingMarginTTM": 0.0134,
        "ReturnOnEquityTTM": -0.0347,
        "ReturnOnAssetsTTM": 0.005,
        "RevenueTTM": 937372992.0,
        "RevenuePerShareTTM": 13.294,
        "GrossProfitTTM": 447644992.0,
        "EBITDA": 358313984.0,
        "DebtToEquity": None,
        "DividendYield": 0.0629,
        "DividendShare": 2.677,
    },
    "Valuation": {"ForwardPE": 67.1141, "PriceBookMRQ": 0.9285, "PriceSalesTTM": 3.5545},
    "SharesStats": {"SharesOutstanding": 71124483, "SharesFloat": 70897596},
    "SplitsDividends": {"PayoutRatio": 153.75},
    "Technicals": {
        "Beta": 1.597,
        "52WeekHigh": 64.1898,
        "52WeekLow": 34.1711,
        "50DayMA": 40.6342,
        "200DayMA": 47.3372,
    },
    "AnalystRatings": {"Rating": 3.6667, "TargetPrice": 47.6111, "Buy": 20, "Hold": 30},
}


class TestExtractSLG:
    def test_does_not_raise_and_keeps_payout_ratio(self):
        cols = _extract_columns(SLG_PAYLOAD)
        # The field that used to overflow NUMERIC(8,6) now survives (fits 12,6).
        assert cols["payout_ratio"] == pytest.approx(153.75)

    def test_core_fields_extracted(self):
        cols = _extract_columns(SLG_PAYLOAD)
        assert cols["symbol"] == "SLG"
        assert cols["sector"] == "Real Estate"
        assert cols["market_cap"] == pytest.approx(3331911168.0)
        assert cols["profit_margin"] == pytest.approx(-0.1617)
        assert cols["forward_pe"] == pytest.approx(67.1141)
        assert cols["pe_ratio"] is None  # PERatio was None
        assert isinstance(cols["shares_outstanding"], int)

    def test_extreme_margin_still_fits_after_widening(self):
        payload = {"Highlights": {"ProfitMargin": -150.0}, "General": {"Code": "X"}}
        cols = _extract_columns(payload)
        assert cols["profit_margin"] == pytest.approx(-150.0)  # fits NUMERIC(12,6)


class TestFitNumeric:
    def test_value_within_bounds(self):
        assert _fit_numeric(153.75, 12, 6, col="payout_ratio") == pytest.approx(153.75)

    def test_old_narrow_column_would_have_rejected(self):
        # The pre-0014 bound: NUMERIC(8,6) caps |value| < 100 → 153.75 -> None.
        assert _fit_numeric(153.75, 8, 6, col="payout_ratio") is None

    def test_overflow_becomes_none(self):
        # NUMERIC(12,6) caps |value| < 1e6.
        assert _fit_numeric(1e9, 12, 6, col="payout_ratio") is None
        assert _fit_numeric(-1e6, 12, 6, col="payout_ratio") is None  # at the bound

    @pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
    def test_non_finite_becomes_none(self, bad):
        assert _fit_numeric(bad, 12, 6, col="x") is None

    @pytest.mark.parametrize("bad", [None, "NA", "abc"])
    def test_missing_or_unparseable_is_none(self, bad):
        assert _fit_numeric(bad, 12, 6, col="x") is None

    def test_rounds_to_scale(self):
        assert _fit_numeric(1.23456789, 12, 6, col="x") == pytest.approx(1.234568)


class TestFitInt:
    def test_within_bounds(self):
        assert _fit_int(71124483, 2**63 - 1, col="shares_outstanding") == 71124483

    def test_overflow_becomes_none(self):
        assert _fit_int(2**40, 2**31 - 1, col="analyst_count") is None

    def test_none_passthrough(self):
        assert _fit_int(None, 2**31 - 1, col="x") is None
