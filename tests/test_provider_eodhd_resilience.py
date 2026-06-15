"""EODHD provider resilience: token redaction + retry policy, plus the
bulk-refresh gap-fill date selection.

These guard the API-efficiency / safety fixes:
  * the api_token must never appear in an exception message (it used to leak
    via httpx's HTTPStatusError URL into the logs),
  * read-timeouts on HEAVY requests (bulk EOD) must NOT be retried (retrying
    a slow huge payload just doubles load); light requests get one retry,
  * 429 rate limits are retried, honoring the server's Retry-After (capped),
  * the bulk refresh must gap-fill from the latest stored bar instead of
    always pulling a fixed trailing window (each bulk call is 100 credits).
"""

from __future__ import annotations

import datetime as dt
from unittest.mock import MagicMock, patch

import httpx
import pytest

from stockscan.data.providers.eodhd import (
    EODHDError,
    EODHDProvider,
    _RateLimited,
    _redact,
    _retry_wait,
    _RetryPolicy,
)
from stockscan.scan.refresh import _bulk_dates


def _state(exc: BaseException | None, attempt: int = 1) -> MagicMock:
    """Build a tenacity retry_state stand-in for policy unit tests."""
    state = MagicMock()
    state.attempt_number = attempt
    if exc is None:
        state.outcome = None
    else:
        state.outcome.failed = True
        state.outcome.exception.return_value = exc
    return state


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


# --- retry policy ----------------------------------------------------------
def test_policy_heavy_never_retries_timeouts():
    policy = _RetryPolicy(heavy=True)
    assert policy(_state(httpx.ReadTimeout("slow"), attempt=1)) is False
    assert policy(_state(httpx.ConnectTimeout("slow"), attempt=1)) is False


def test_policy_light_retries_timeout_once():
    policy = _RetryPolicy(heavy=False)
    assert policy(_state(httpx.ReadTimeout("slow"), attempt=1)) is True
    assert policy(_state(httpx.ReadTimeout("slow"), attempt=2)) is False


def test_policy_retries_transient_server_errors_twice():
    policy = _RetryPolicy()
    req = httpx.Request("GET", "https://x")
    err = httpx.HTTPStatusError("500", request=req, response=httpx.Response(500, request=req))
    assert policy(_state(httpx.ConnectError("refused"), attempt=1)) is True
    assert policy(_state(err, attempt=2)) is True
    assert policy(_state(err, attempt=3)) is False


def test_policy_retries_rate_limits():
    policy = _RetryPolicy(heavy=True)  # heaviness doesn't matter for 429s
    assert policy(_state(_RateLimited(5.0), attempt=1)) is True
    assert policy(_state(_RateLimited(5.0), attempt=2)) is True
    assert policy(_state(_RateLimited(5.0), attempt=3)) is False


def test_policy_ignores_unrelated_errors():
    assert _RetryPolicy()(_state(ValueError("nope"))) is False


def test_retry_wait_honors_retry_after_capped():
    assert _retry_wait(_state(_RateLimited(7.0))) == 7.0
    assert _retry_wait(_state(_RateLimited(600.0))) == 30.0  # capped


# --- 429 end-to-end --------------------------------------------------------
def test_provider_retries_429_then_succeeds(monkeypatch):
    # Make the rate-limit wait instant so the test doesn't sleep.
    monkeypatch.setattr(
        "stockscan.data.providers.eodhd._retry_wait", lambda _state: 0.0
    )
    calls = {"n": 0}

    def _handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "1"}, text="slow down")
        return httpx.Response(200, json=[])

    p = EODHDProvider(
        api_key="k",
        base_url="https://eodhd.test/api",
        transport=httpx.MockTransport(_handler),
    )
    assert p.get_eod_bulk(dt.date(2026, 6, 5)) == []
    assert calls["n"] == 2


def test_provider_429_exhaustion_raises_eodhd_error(monkeypatch):
    monkeypatch.setattr(
        "stockscan.data.providers.eodhd._retry_wait", lambda _state: 0.0
    )

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"retry-after": "1"}, text="slow down")

    p = EODHDProvider(
        api_key="SUPERSECRETKEY",
        base_url="https://eodhd.test/api",
        transport=httpx.MockTransport(_handler),
    )
    with pytest.raises(EODHDError) as ei:
        p.get_eod_bulk(dt.date(2026, 6, 5))
    msg = str(ei.value)
    assert "429" in msg
    assert "SUPERSECRETKEY" not in msg


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
