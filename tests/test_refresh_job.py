"""Background Fetch-Latest job: lifecycle, single-flight, route polling."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from stockscan.scan import refresh_job
from stockscan.scan.refresh_job import (
    consume_finished,
    current_job,
    start_refresh,
)


@pytest.fixture(autouse=True)
def _clean_job_state():
    refresh_job._reset_for_tests()
    yield
    refresh_job._reset_for_tests()


def _wait_done(timeout: float = 5.0) -> None:
    """Spin until the current job leaves the running state."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = current_job()
        if job is None or job.status != "running":
            return
        time.sleep(0.01)
    raise AssertionError("job did not finish in time")


# ----------------------------------------------------------------------
# Lifecycle
# ----------------------------------------------------------------------


def test_job_success_records_summary(monkeypatch):
    monkeypatch.setattr(
        refresh_job, "_do_refresh", lambda *, days_back: {"signals_emitted": 3}
    )
    _job, started = start_refresh()
    assert started is True
    _wait_done()
    finished = consume_finished()
    assert finished is not None
    assert finished.status == "done"
    assert finished.summary == {"signals_emitted": 3}
    assert finished.error is None
    # consume is one-shot
    assert consume_finished() is None
    assert current_job() is None


def test_job_failure_records_error(monkeypatch):
    def _boom(*, days_back):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(refresh_job, "_do_refresh", _boom)
    start_refresh()
    _wait_done()
    finished = consume_finished()
    assert finished is not None
    assert finished.status == "error"
    assert "db exploded" in (finished.error or "")


def test_single_flight_joins_running_job(monkeypatch):
    release = threading.Event()

    def _slow(*, days_back):
        release.wait(5)
        return {}

    monkeypatch.setattr(refresh_job, "_do_refresh", _slow)
    first, started_first = start_refresh()
    second, started_second = start_refresh()
    assert started_first is True
    assert started_second is False
    assert second is first  # same job snapshot
    release.set()
    _wait_done()
    assert consume_finished() is not None


def test_consume_returns_none_while_running(monkeypatch):
    release = threading.Event()
    def _slow_refresh(*, days_back):
        release.wait(5)
        return {}

    monkeypatch.setattr(refresh_job, "_do_refresh", _slow_refresh)
    start_refresh()
    assert consume_finished() is None  # still running — must not pop
    release.set()
    _wait_done()
    assert consume_finished() is not None


# ----------------------------------------------------------------------
# Routes: POST /signals/refresh starts + polls, GET /status completes
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


def test_refresh_post_returns_polling_strip(client, monkeypatch):
    release = threading.Event()
    def _slow_refresh(*, days_back):
        release.wait(5)
        return {}

    monkeypatch.setattr(refresh_job, "_do_refresh", _slow_refresh)
    # Avoid tripping the rate limiter from other tests' state.
    monkeypatch.setattr(
        "stockscan.web.routes.signals.rate_limit_check", lambda *a, **k: None
    )
    r = client.post("/signals/refresh")
    assert r.status_code == 200
    assert 'id="refresh-status"' in r.text  # polling strip is in the content
    assert "/signals/refresh/status" in r.text
    release.set()
    _wait_done()


def test_refresh_status_running_then_done(client, monkeypatch):
    release = threading.Event()
    def _slow_refresh(*, days_back):
        release.wait(5)
        return {"signals_emitted": 2, "up_to_date": False}

    monkeypatch.setattr(refresh_job, "_do_refresh", _slow_refresh)
    monkeypatch.setattr(
        "stockscan.web.routes.signals.rate_limit_check", lambda *a, **k: None
    )
    client.post("/signals/refresh")

    # While running: small self-polling fragment, not the whole page.
    r = client.get("/signals/refresh/status")
    assert r.status_code == 200
    assert 'id="refresh-status"' in r.text
    assert 'id="signals-content"' not in r.text

    release.set()
    _wait_done()

    # After completion: full content + retarget headers + result consumed.
    r2 = client.get("/signals/refresh/status")
    assert 'id="signals-content"' in r2.text
    assert r2.headers.get("HX-Retarget") == "#signals-content"
    assert current_job() is None


def test_second_post_joins_inflight_job(client, monkeypatch):
    release = threading.Event()
    def _slow_refresh(*, days_back):
        release.wait(5)
        return {}

    monkeypatch.setattr(refresh_job, "_do_refresh", _slow_refresh)
    monkeypatch.setattr(
        "stockscan.web.routes.signals.rate_limit_check", lambda *a, **k: None
    )
    client.post("/signals/refresh")
    r2 = client.post("/signals/refresh")
    assert r2.status_code == 200
    assert 'id="refresh-status"' in r2.text  # joined, still polling
    release.set()
    _wait_done()


def test_status_without_job_renders_content(client):
    r = client.get("/signals/refresh/status")
    assert r.status_code == 200
    assert 'id="signals-content"' in r.text
