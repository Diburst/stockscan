"""Backtests page: run form, POST /backtests/run, status fragment states."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from stockscan.backtest import job
from stockscan.backtest.job import BacktestJobState
from stockscan.web.app import create_app
from stockscan.web.deps import get_session
from stockscan.web.routes import backtests as backtests_route


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


@pytest.fixture(autouse=True)
def _clean_job_state():
    job._reset_for_tests()
    yield
    job._reset_for_tests()


@pytest.fixture
def client(monkeypatch) -> TestClient:
    monkeypatch.setattr(backtests_route, "list_runs", lambda **k: [])
    app = create_app()
    app.dependency_overrides[get_session] = _mock_session
    return TestClient(app, raise_server_exceptions=True)


def _state(status: str = "running", **overrides) -> BacktestJobState:
    fields = dict(
        status=status,
        started_at=datetime.now(UTC),
        strategy="rsi2_meanrev",
        start=date(2021, 1, 1),
        end=date(2026, 1, 1),
        capital=100_000.0,
        slippage_bps=5.0,
        commission=0.0,
        symbols=None,
    )
    fields.update(overrides)
    return BacktestJobState(**fields)


def _install(monkeypatch, state: BacktestJobState | None) -> None:
    """Put a job state in the module as if a thread had produced it."""
    monkeypatch.setattr(job, "_CURRENT", state)


# ----------------------------------------------------------------------
# Form + list
# ----------------------------------------------------------------------


def test_list_renders_run_form(client):
    r = client.get("/backtests")
    assert r.status_code == 200
    assert "Run a backtest" in r.text
    assert 'name="strategy"' in r.text
    assert 'value="rsi2_meanrev"' in r.text
    assert 'value="momentum_52w_high"' in r.text
    assert 'id="backtest-run-status"' in r.text
    assert "No backtest running" in r.text


def test_list_shows_metrics_columns(client, monkeypatch):
    monkeypatch.setattr(backtests_route, "list_runs", lambda **k: [{
        "run_id": 3, "strategy_name": "rsi2_meanrev", "strategy_version": "2.0.0",
        "start_date": date(2021, 1, 1), "end_date": date(2026, 1, 1),
        "num_trades": 12, "ending_equity": 123456,
        "metrics_json": {"cagr": 0.1234, "sharpe": 1.5},
        "created_at": datetime(2026, 9, 1, 12, 0),
    }])
    r = client.get("/backtests")
    assert "12.3%" in r.text
    assert "1.50" in r.text


def test_list_joins_running_job(client, monkeypatch):
    _install(monkeypatch, _state())
    r = client.get("/backtests")
    assert 'hx-trigger="every 2s"' in r.text
    assert "Running" in r.text
    assert "historical S&amp;P 500" in r.text


# ----------------------------------------------------------------------
# POST /backtests/run
# ----------------------------------------------------------------------


def test_post_starts_job_with_parsed_form(client, monkeypatch):
    calls = []

    def _fake_start(**kwargs):
        calls.append(kwargs)
        return _state(strategy=kwargs["strategy"]), True

    monkeypatch.setattr(backtests_route, "start_backtest", _fake_start)
    r = client.post(
        "/backtests/run",
        data={
            "strategy": "momentum_52w_high", "start": "2022-01-01", "end": "",
            "capital": "50000", "slippage_bps": "3", "commission": "0.5",
            "symbols": "aapl, msft nvda aapl", "note": "  web run ",
        },
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    assert calls == [{
        "strategy": "momentum_52w_high",
        "start": date(2022, 1, 1),
        "end": None,
        "capital": 50000.0,
        "slippage_bps": 3.0,
        "commission": 0.5,
        "symbols": ["AAPL", "MSFT", "NVDA"],
        "note": "web run",
    }]
    assert 'id="backtest-run-status"' in r.text
    assert "started" in r.headers["HX-Trigger"]


def test_post_without_htmx_redirects_with_flash(client, monkeypatch):
    monkeypatch.setattr(
        backtests_route, "start_backtest", lambda **kw: (_state(), True)
    )
    r = client.post(
        "/backtests/run", data={"strategy": "rsi2_meanrev"}, follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/backtests"
    assert "set-cookie" in r.headers


def test_post_while_running_does_not_start_another(client, monkeypatch):
    _install(monkeypatch, _state())
    started = MagicMock()
    monkeypatch.setattr(backtests_route, "start_backtest", started)
    r = client.post(
        "/backtests/run", data={"strategy": "rsi2_meanrev"},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    started.assert_not_called()
    assert 'hx-trigger="every 2s"' in r.text
    assert "already running" in r.headers["HX-Trigger"]


def test_post_rejects_bad_inputs(client, monkeypatch):
    started = MagicMock()
    monkeypatch.setattr(backtests_route, "start_backtest", started)
    hx = {"HX-Request": "true"}
    r = client.post("/backtests/run", data={"strategy": "rsi2_meanrev", "start": "2026-01-01", "end": "2025-01-01"}, headers=hx)
    assert "before end" in r.headers["HX-Trigger"]
    r = client.post("/backtests/run", data={"strategy": "rsi2_meanrev", "start": "not-a-date"}, headers=hx)
    assert "ISO" in r.headers["HX-Trigger"]
    r = client.post("/backtests/run", data={"strategy": "rsi2_meanrev", "capital": "0"}, headers=hx)
    assert "positive" in r.headers["HX-Trigger"]
    started.assert_not_called()


def test_post_unknown_strategy(client):
    r = client.post(
        "/backtests/run", data={"strategy": "nope"}, headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    assert "Unknown strategy" in r.headers["HX-Trigger"]
    assert job.current_job() is None


# ----------------------------------------------------------------------
# GET /backtests/run/status
# ----------------------------------------------------------------------


def test_status_idle(client):
    r = client.get("/backtests/run/status")
    assert r.status_code == 200
    assert "No backtest running" in r.text
    assert "hx-trigger" not in r.text


def test_status_running_polls(client, monkeypatch):
    _install(monkeypatch, _state(symbols=("AAPL", "MSFT")))
    r = client.get("/backtests/run/status")
    assert 'hx-get="/backtests/run/status"' in r.text
    assert 'hx-trigger="every 2s"' in r.text
    assert "2 symbols" in r.text
    assert job.current_job() is not None  # not consumed while running


def test_status_done_links_run_and_consumes(client, monkeypatch):
    _install(monkeypatch, _state("done", finished_at=datetime.now(UTC), run_id=17))
    r = client.get("/backtests/run/status")
    assert 'href="/backtests/17"' in r.text
    assert "View run #17" in r.text
    assert "hx-trigger" not in r.text
    assert "run #17" in r.headers["HX-Trigger"]
    assert job.current_job() is None
    r2 = client.get("/backtests/run/status")
    assert "No backtest running" in r2.text


def test_status_error(client, monkeypatch):
    _install(
        monkeypatch,
        _state("error", finished_at=datetime.now(UTC), error="Backtest failed: no bars"),
    )
    r = client.get("/backtests/run/status")
    assert "no bars" in r.text
    assert "failed" in r.text
    assert "hx-trigger" not in r.text
    assert job.current_job() is None
