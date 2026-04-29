"""FredProvider: parse FRED /series/observations into canonical MacroRow.

Mocks the network with ``httpx.MockTransport``. The injected transport
is wired through the new ``transport`` kwarg on ``FredProvider``.

The motivating series is ``BAMLH0A0HYM2`` (HY OAS) for the v2 regime
composite's credit component, but nothing in this provider is series-
specific — any FRED series code works.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import httpx
import pytest

from stockscan.data.providers.base import MacroRow
from stockscan.data.providers.fred import FredError, FredProvider

# A canned response that mimics one week of HY OAS values from
# FRED /series/observations, including FRED's missing-data sentinel ('.').
_OBSERVATIONS_FIXTURE: dict[str, Any] = {
    "realtime_start": "2026-04-27",
    "realtime_end": "2026-04-27",
    "observation_start": "2026-04-20",
    "observation_end": "2026-04-24",
    "units": "lin",
    "output_type": 1,
    "file_type": "json",
    "order_by": "observation_date",
    "sort_order": "asc",
    "count": 5,
    "offset": 0,
    "limit": 100000,
    "observations": [
        {
            "realtime_start": "2026-04-27",
            "realtime_end": "2026-04-27",
            "date": "2026-04-20",
            "value": "3.45",
        },
        {
            "realtime_start": "2026-04-27",
            "realtime_end": "2026-04-27",
            "date": "2026-04-21",
            "value": "3.52",
        },
        # Missing-data sentinel — must be skipped.
        {
            "realtime_start": "2026-04-27",
            "realtime_end": "2026-04-27",
            "date": "2026-04-22",
            "value": ".",
        },
        {
            "realtime_start": "2026-04-27",
            "realtime_end": "2026-04-27",
            "date": "2026-04-23",
            "value": "3.61",
        },
        {
            "realtime_start": "2026-04-27",
            "realtime_end": "2026-04-27",
            "date": "2026-04-24",
            "value": "3.58",
        },
    ],
}


def _capturing_transport(
    captured: list[httpx.Request],
    response_payload: Any = _OBSERVATIONS_FIXTURE,
    status_code: int = 200,
) -> httpx.MockTransport:
    """MockTransport that records each request and returns a canned payload."""

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(status_code, json=response_payload)

    return httpx.MockTransport(_handler)


def _provider(transport: httpx.MockTransport) -> FredProvider:
    return FredProvider(
        api_key="test-key",
        base_url="https://fred.test/fred",
        transport=transport,
    )


# --------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------
def test_get_macro_series_returns_canonical_macrorows() -> None:
    p = _provider(_capturing_transport([]))
    rows = p.get_macro_series("BAMLH0A0HYM2", date(2026, 4, 20), date(2026, 4, 24))
    # 5 observations in fixture, 1 is "." — expect 4 valid rows.
    assert len(rows) == 4
    assert all(isinstance(r, MacroRow) for r in rows)
    assert all(r.series_code == "BAMLH0A0HYM2" for r in rows)
    assert all(r.source == "fred" for r in rows)
    assert rows[0].as_of_date == date(2026, 4, 20)
    assert rows[0].value == Decimal("3.45")
    assert rows[-1].as_of_date == date(2026, 4, 24)
    assert rows[-1].value == Decimal("3.58")


def test_request_targets_observations_endpoint_with_required_params() -> None:
    captured: list[httpx.Request] = []
    p = _provider(_capturing_transport(captured))
    p.get_macro_series("BAMLH0A0HYM2", date(2026, 4, 20), date(2026, 4, 24))
    assert len(captured) == 1
    req = captured[0]
    assert req.url.path == "/fred/series/observations"
    qp = dict(req.url.params)
    assert qp["series_id"] == "BAMLH0A0HYM2"
    assert qp["observation_start"] == "2026-04-20"
    assert qp["observation_end"] == "2026-04-24"
    assert qp["file_type"] == "json"
    assert qp["api_key"] == "test-key"


# --------------------------------------------------------------------------
# Missing / malformed observation handling
# --------------------------------------------------------------------------
def test_dot_sentinel_observations_are_skipped() -> None:
    """FRED uses ``.`` to flag missing values — these must NOT become rows."""
    p = _provider(_capturing_transport([]))
    rows = p.get_macro_series("BAMLH0A0HYM2", date(2026, 4, 20), date(2026, 4, 24))
    dropped_date = date(2026, 4, 22)
    assert dropped_date not in {r.as_of_date for r in rows}


def test_malformed_value_is_skipped_not_raised() -> None:
    payload: dict[str, Any] = {
        "observations": [
            {"date": "2026-04-20", "value": "3.45"},
            {"date": "2026-04-21", "value": "not-a-number"},  # malformed
            {"date": "not-a-date", "value": "3.50"},  # malformed
            {"date": "2026-04-22", "value": "3.55"},
        ],
    }
    p = _provider(_capturing_transport([], response_payload=payload))
    rows = p.get_macro_series("X", date(2026, 4, 20), date(2026, 4, 22))
    assert len(rows) == 2  # only the two well-formed rows
    assert rows[0].value == Decimal("3.45")
    assert rows[1].value == Decimal("3.55")


def test_empty_observations_returns_empty_list() -> None:
    p = _provider(_capturing_transport([], response_payload={"observations": []}))
    rows = p.get_macro_series("X", date(2026, 4, 20), date(2026, 4, 24))
    assert rows == []


# --------------------------------------------------------------------------
# Error handling
# --------------------------------------------------------------------------
def test_construction_raises_when_api_key_missing() -> None:
    """No api_key, no provider — caller must handle this at startup.

    conftest.py forces ``FRED_API_KEY=""`` so the settings singleton has
    no fallback either. Passing ``api_key=""`` therefore exercises the
    raise-on-empty branch.
    """
    with pytest.raises(FredError, match="FRED_API_KEY is not set"):
        FredProvider(api_key="")


def test_4xx_response_raises_fred_error() -> None:
    p = _provider(_capturing_transport([], response_payload={"error": "bad"}, status_code=400))
    with pytest.raises(FredError, match="FRED 400"):
        p.get_macro_series("X", date(2026, 4, 20), date(2026, 4, 24))


def test_unexpected_payload_shape_raises() -> None:
    """If FRED returns something that isn't a dict (e.g., HTML error page
    that happens to be valid JSON), surface it instead of silently returning
    an empty list."""
    p = _provider(_capturing_transport([], response_payload=["not", "a", "dict"]))
    with pytest.raises(FredError, match="unexpected payload shape"):
        p.get_macro_series("X", date(2026, 4, 20), date(2026, 4, 24))
