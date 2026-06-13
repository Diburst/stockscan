"""EODHD provider resilience: token redaction + retry predicate, plus the
bulk-refresh gap-fill date selection.

These guard the API-efficiency / safety fixes:
  * the api_token must never appear in an exception message (it used to leak
    via httpx's HTTPStatusError URL into the logs),
  * read-timeouts must NOT be retried (retrying a slow huge payload just
    doubles load),
  * the bulk refresh must gap-fill from the latest stored bar instead of
    always pulling a fixed trailing window (each bulk call is 100 credits).
"""

from __future__ import annotations

import datetime as dt
from unittest.mock import patch

import httpx
import pytest

from stockscan.data.providers.eodhd import (
    EODHDError,
    EODHDProvider,
    _redact,
    _should_retry,
)
from stockscan.scan.refresh import _bulk_dates


# --- token redaction -------------------------------------------------------
def test_redact_masks_token():
    s = "Server error '500' for url 'https://eodhd.com/api/x?date=2026-06-05&api_token=69f0.86196050&fmt=json'"
    out = _redact(s)
    assert "69f0.86196050" not in out
    assert "api_token=***" in out


def test_provider_redacts_token_on_5xx():
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    p = EODHDProvider(
        api_key="SUPERSECRETKEY",
        base_url="https://eodhd.test/api",
        transport=httpx.MockTransport(_handler),
    )
    with pytest.raises(EODHDError) as ei:
        p.get_eod_bulk(dt.date(2026, 6, 5))
    msg = str(ei.value)
    assert "SUPERSECRETKEY" not in msg          # the key never escapes
    assert "api_token=***" in msg               # ...it's masked instead


# --- retry predicate -------------------------------------------------------
def test_should_retry_skips_read_timeouts():
    assert _should_retry(httpx.ReadTimeout("slow")) is False
    assert _should_retry(httpx.ConnectTimeout("slow")) is False


def test_should_retry_allows_transient_server_errors():
    assert _should_retry(httpx.ConnectError("refused")) is True
    req = httpx.Request("GET", "https://x")
    err = httpx.HTTPStatusError("500", request=req, response=httpx.Response(500, request=req))
    assert _should_retry(err) is True


def test_should_retry_ignores_unrelated_errors():
    assert _should_retry(ValueError("nope")) is False


# --- bulk-refresh gap-fill -------------------------------------------------
def test_bulk_dates_full_window_when_store_empty():
    with patch("stockscan.data.store.latest_daily_bar_date", return_value=None):
        dates = _bulk_dates(7)
    # A 7-day window always contains at least 5 weekdays.
    assert len(dates) >= 4


def test_bulk_dates_gapfills_when_current():
    today = dt.date.today()
    with patch("stockscan.data.store.latest_daily_bar_date", return_value=today):
        dates = _bulk_dates(7)
    # Store already current → only the single overlap day (or none on a
    # weekend), never the whole trailing window.
    assert len(dates) <= 1


def test_bulk_dates_caps_long_outage_at_days_back():
    stale = dt.date.today() - dt.timedelta(days=90)
    with patch("stockscan.data.store.latest_daily_bar_date", return_value=stale):
        capped = _bulk_dates(7)
    with patch("stockscan.data.store.latest_daily_bar_date", return_value=None):
        full = _bulk_dates(7)
    # A 90-day-stale store doesn't balloon — it's clamped to the days_back floor.
    assert capped == full
