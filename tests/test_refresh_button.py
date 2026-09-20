"""The one Refresh button: the background job's lifecycle and single-flight
guarantee, and the strip's three states over POST /refresh + GET /refresh/status."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from datetime import date
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from stockscan.jobs import background
from stockscan.jobs.pipeline import STEPS, PipelineResult


@pytest.fixture(autouse=True)
def _clean_job_state():
    background._reset_for_tests()
    yield
    background._reset_for_tests()


def _result(**overrides) -> PipelineResult:
    base = dict(
        as_of=date(2026, 9, 18), bars_upserted=1283, caught_up=("AAOI",), scans=[], scans_skipped=True,
        regime=None, trades_marked=0, trades_auto_closed=0, options_run_id=4, options_settled=2,
        feeds={"news": "12 articles", "earnings": "not on plan"}, watchlist_alerts_fired=0,
        step_failures=[], duration_seconds=34.2,
    )
    base.update(overrides)
    return PipelineResult(**base)


def _wait_done(timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = background.current()
        if job is None or not job.running:
            return
        time.sleep(0.01)
    raise AssertionError("job did not finish in time")


def _slow(release: threading.Event, result: PipelineResult | None = None, error: Exception | None = None):
    """A stand-in pipeline that reports one step, then blocks until released."""

    def _run(*, send_summary, progress):
        assert send_summary is False
        progress("regime", 3, len(STEPS))
        release.wait(5)
        if error:
            raise error
        return result or _result()

    return _run


# ----------------------------------------------------------------------
# Lifecycle
# ----------------------------------------------------------------------
def test_job_records_result_and_steps(monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(background, "run_pipeline", _slow(release))
    state, started = background.start()
    assert started is True and state.running and state.step == STEPS[0]
    time.sleep(0.05)
    assert background.current().step == "regime" and background.current().step_index == 3
    release.set()
    _wait_done()
    job = background.current()
    assert job.status == "done" and job.error is None
    assert job.result.bars_upserted == 1283 and job.finished_at is not None


def test_job_failure_records_error(monkeypatch):
    release = threading.Event()
    release.set()
    monkeypatch.setattr(background, "run_pipeline", _slow(release, error=RuntimeError("db down")))
    background.start()
    _wait_done()
    job = background.current()
    assert job.status == "error" and job.error == "db down" and job.result is None


def test_single_flight_joins_running_job(monkeypatch):
    release = threading.Event()
    calls = []
    real = _slow(release)

    def _counted(**kw):
        calls.append(1)
        return real(**kw)

    monkeypatch.setattr(background, "run_pipeline", _counted)
    first, started_first = background.start()
    second, started_second = background.start()
    assert started_first is True and started_second is False
    assert second.started_at == first.started_at
    release.set()
    _wait_done()
    assert len(calls) == 1
    # A finished job does not block the next start.
    release.clear()
    _third, started_third = background.start()
    assert started_third is True
    release.set()
    _wait_done()


# ----------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------
def _empty_result():
    res = MagicMock()
    res.first.return_value = None
    res.one.return_value = None
    res.all.return_value = []
    res.__iter__ = lambda self: iter([])
    return res


def _mock_session() -> Iterator[MagicMock]:
    s = MagicMock()
    s.execute.return_value = _empty_result()
    yield s


@pytest.fixture
def client() -> TestClient:
    from stockscan.web.app import create_app
    from stockscan.web.deps import get_session

    app = create_app()
    app.dependency_overrides[get_session] = _mock_session
    return TestClient(app, raise_server_exceptions=True)


def test_post_starts_and_returns_the_running_strip(client, monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(background, "run_pipeline", _slow(release))
    r = client.post("/refresh")
    assert r.status_code == 200
    assert 'id="refresh-strip"' in r.text
    assert "hx-get=\"/refresh/status?was_running=true\"" in r.text
    assert "Refreshing…" in r.text
    release.set()
    _wait_done()


def test_second_post_joins_without_starting_another_run(client, monkeypatch):
    release = threading.Event()
    calls = []
    real = _slow(release)

    def _counted(**kw):
        calls.append(1)
        return real(**kw)

    monkeypatch.setattr(background, "run_pipeline", _counted)
    client.post("/refresh")
    r2 = client.post("/refresh")
    assert r2.status_code == 200 and "Refreshing…" in r2.text
    release.set()
    _wait_done()
    assert len(calls) == 1


def test_status_polls_then_reports_the_result_and_fires_refresh_done(client, monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(background, "run_pipeline", _slow(release))
    client.post("/refresh")

    r = client.get("/refresh/status?was_running=true")
    assert "step 3/%d" % len(STEPS) in r.text and "regime" in r.text
    assert "HX-Trigger" not in r.headers

    release.set()
    _wait_done()
    r2 = client.get("/refresh/status?was_running=true")
    assert r2.headers.get("HX-Trigger") == "refresh-done"
    assert "/refresh/status" not in r2.text  # polling stops
    assert "1,283 bars (caught up AAOI)" in r2.text
    assert "scans skipped, nothing new" in r2.text
    assert "options book saved, 2 settled" in r2.text
    assert "news 12 articles" in r2.text and "earnings" not in r2.text
    assert "Refreshing…" not in r2.text  # the button is live again

    # A later poll without the running flag does not re-fire the reload.
    r3 = client.get("/refresh/status")
    assert "HX-Trigger" not in r3.headers


def test_status_shows_failure(client, monkeypatch):
    release = threading.Event()
    release.set()
    monkeypatch.setattr(background, "run_pipeline", _slow(release, error=RuntimeError("db down")))
    client.post("/refresh")
    _wait_done()
    r = client.get("/refresh/status?was_running=true")
    assert "failed" in r.text and "db down" in r.text


def test_degraded_run_lists_step_failures(client, monkeypatch):
    release = threading.Event()
    release.set()
    monkeypatch.setattr(
        background, "run_pipeline", _slow(release, result=_result(step_failures=["macro refresh: BAMLH0A0HYM2"]))
    )
    client.post("/refresh")
    _wait_done()
    r = client.get("/refresh/status")
    assert "degraded" in r.text and "macro refresh: BAMLH0A0HYM2" in r.text


def test_idle_strip_has_a_live_button_and_no_poll(client):
    r = client.get("/refresh/status")
    assert r.status_code == 200
    assert 'id="refresh-strip"' in r.text and "/refresh/status" not in r.text and "Refreshing…" not in r.text
