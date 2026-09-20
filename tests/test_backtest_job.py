"""Background backtest job: lifecycle, single-flight, CLI-equivalent config."""

from __future__ import annotations

import threading
import time
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from stockscan.backtest import job
from stockscan.backtest.job import (
    DEFAULT_WINDOW_DAYS,
    consume_finished,
    current_job,
    start_backtest,
)


@pytest.fixture(autouse=True)
def _clean_job_state():
    job._reset_for_tests()
    yield
    job._reset_for_tests()


def _wait_done(timeout: float = 5.0) -> None:
    """Spin until the current job leaves the running state."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = current_job()
        if state is None or state.status != "running":
            return
        time.sleep(0.01)
    raise AssertionError("job did not finish in time")


def _patch_engine(monkeypatch, *, run_id: int = 42):
    """Stub the engine + store; returns the engine mock and the save mock."""
    engine_cls = MagicMock(name="BacktestEngine")
    save = MagicMock(name="save_run", return_value=run_id)
    monkeypatch.setattr(job, "BacktestEngine", engine_cls)
    monkeypatch.setattr(job, "save_run", save)
    return engine_cls, save


# ----------------------------------------------------------------------
# Lifecycle
# ----------------------------------------------------------------------


def test_job_success_records_run_id(monkeypatch):
    engine_cls, save = _patch_engine(monkeypatch, run_id=7)
    _state, started = start_backtest(strategy="rsi2_meanrev", note="from the web")
    assert started is True
    _wait_done()
    finished = consume_finished()
    assert finished is not None
    assert finished.status == "done"
    assert finished.run_id == 7
    assert finished.error is None
    save.assert_called_once_with(engine_cls.return_value.run.return_value, note="from the web")
    # consume is one-shot
    assert consume_finished() is None
    assert current_job() is None


def test_job_builds_config_like_the_cli(monkeypatch):
    engine_cls, _save = _patch_engine(monkeypatch)
    start_backtest(
        strategy="momentum_52w_high",
        start=date(2021, 1, 4),
        end=date(2024, 12, 31),
        capital=250_000.0,
        slippage_bps=7.5,
        commission=1.0,
        symbols=["AAPL", "MSFT"],
    )
    _wait_done()
    cfg = engine_cls.call_args.args[0]
    assert cfg.strategy_cls.name == "momentum_52w_high"
    assert (cfg.start_date, cfg.end_date) == (date(2021, 1, 4), date(2024, 12, 31))
    assert cfg.starting_capital == Decimal("250000.0")
    assert cfg.commission_per_trade == Decimal("1.0")
    assert cfg.slippage.bps == Decimal("7.5")
    assert cfg.universe == ["AAPL", "MSFT"]


def test_job_defaults_match_the_cli(monkeypatch):
    engine_cls, _save = _patch_engine(monkeypatch)
    state, _started = start_backtest(strategy="rsi2_meanrev")
    assert state.end == date.today()
    assert state.start == date.today() - timedelta(days=DEFAULT_WINDOW_DAYS)
    assert state.symbols is None
    assert state.universe_label == "historical S&P 500"
    _wait_done()
    cfg = engine_cls.call_args.args[0]
    assert cfg.universe is None
    assert cfg.starting_capital == Decimal("100000.0")
    assert cfg.slippage.bps == Decimal("5.0")
    assert cfg.commission_per_trade == Decimal("0.0")


def test_unknown_strategy_raises_before_starting():
    with pytest.raises(KeyError):
        start_backtest(strategy="nope")
    assert current_job() is None


def test_job_failure_records_error(monkeypatch):
    engine_cls, save = _patch_engine(monkeypatch)
    engine_cls.return_value.run.side_effect = RuntimeError("no bars")
    start_backtest(strategy="rsi2_meanrev")
    _wait_done()
    finished = consume_finished()
    assert finished is not None
    assert finished.status == "error"
    assert finished.run_id is None
    assert "no bars" in (finished.error or "")
    save.assert_not_called()


def test_single_flight_reports_running_job(monkeypatch):
    engine_cls, _save = _patch_engine(monkeypatch)
    release = threading.Event()
    engine_cls.return_value.run.side_effect = lambda: release.wait(5)
    first, started_first = start_backtest(strategy="rsi2_meanrev")
    second, started_second = start_backtest(strategy="momentum_52w_high")
    assert started_first is True
    assert started_second is False
    assert second is first
    assert consume_finished() is None  # still running — must not pop
    release.set()
    _wait_done()
    assert consume_finished() is not None
