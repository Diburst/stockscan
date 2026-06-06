"""Unit tests for the macro event-type importance bucketing.

No DB, no provider — pure-function tests over the classifier so the
dashboard's importance filter behaves predictably as the EODHD feed
adds new event types.
"""

from __future__ import annotations

import pytest

from stockscan.econ_events.importance import classify_importance


@pytest.mark.parametrize(
    "event_type",
    [
        "CPI",
        "Core CPI",
        "Nonfarm Payrolls",
        "Non-Farm Payrolls",
        "Unemployment Rate",
        "FOMC Economic Projections",
        "FOMC Statement",
        "Federal Funds Rate",
        "Interest Rate Decision",
        "PCE Price Index",
        "Core PCE Price Index",
        "PPI",
    ],
)
def test_high_importance_buckets(event_type: str) -> None:
    """Equity-market movers all map to high."""
    assert classify_importance(event_type) == "high"


@pytest.mark.parametrize(
    "event_type",
    [
        "ISM Manufacturing PMI",
        "ISM Services PMI",
        "S&P Global Manufacturing PMI",
        "Retail Sales MoM",
        "GDP Growth Rate",
        "JOLTS Job Openings",
        "Initial Jobless Claims",
        "Continuing Jobless Claims",
        "Consumer Confidence",
        "Michigan Consumer Sentiment",
        "Durable Goods Orders",
        "Industrial Production",
        "Housing Starts",
        "New Home Sales",
        "Existing Home Sales",
        "Trade Balance",
        "Factory Orders",
    ],
)
def test_medium_importance_buckets(event_type: str) -> None:
    """Second-tier prints map to medium."""
    assert classify_importance(event_type) == "medium"


@pytest.mark.parametrize(
    "event_type",
    [
        "10-Year Bond Auction",
        "2-Year Note Auction",
        "Mortgage Applications",
        "API Crude Oil Stock Change",
        "Capital Expenditure",
        "Some Brand New EODHD Event Type",
    ],
)
def test_low_importance_default(event_type: str) -> None:
    """Unknown / fall-through event types default to low."""
    assert classify_importance(event_type) == "low"


def test_none_falls_through_to_low() -> None:
    assert classify_importance(None) == "low"


def test_empty_string_falls_through_to_low() -> None:
    assert classify_importance("") == "low"


def test_case_insensitive() -> None:
    """Lowercase event type still matches the bucket."""
    assert classify_importance("cpi mom") == "high"
    assert classify_importance("ism services pmi") == "medium"
