"""Background-job wrapper around a single backtest run.

The Backtests page's "Run a backtest" form used to be the CLI only — a run
takes minutes on the full S&P 500, far too long to hold a request open. The
POST starts the work on a daemon thread and returns immediately; the page
polls ``GET /backtests/run/status`` every 2 s until the run lands, then
shows a link to the saved report.

Same constraints as :mod:`stockscan.scan.refresh_job`: in-process,
single-flight (one backtest at a time, guarded by a lock — a second POST
while one runs is told so rather than starting another), state in module
globals (single uvicorn worker), and the thread opens its own DB session
via :func:`stockscan.backtest.store.save_run`.

The config is built exactly as ``stockscan backtest run`` builds it, so a
form run and a CLI run with the same inputs land in ``backtest_runs`` with
the same idempotency key. The engine's own run log reports progress; there
is no extra progress plumbing here.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Literal

from stockscan.backtest.engine import BacktestConfig, BacktestEngine
from stockscan.backtest.slippage import FixedBpsSlippage
from stockscan.backtest.store import save_run
from stockscan.strategies import STRATEGY_REGISTRY, discover_strategies

log = logging.getLogger(__name__)

DEFAULT_CAPITAL = 100_000.0
DEFAULT_SLIPPAGE_BPS = 5.0
DEFAULT_COMMISSION = 0.0
DEFAULT_WINDOW_DAYS = 5 * 365


@dataclass(frozen=True, slots=True)
class BacktestJobState:
    """Immutable snapshot of the current/most-recent backtest job."""

    status: Literal["running", "done", "error"]
    started_at: datetime
    strategy: str
    start: date
    end: date
    capital: float
    slippage_bps: float
    commission: float
    symbols: tuple[str, ...] | None  # None = point-in-time S&P 500
    note: str | None = None
    finished_at: datetime | None = None
    run_id: int | None = None
    error: str | None = None

    @property
    def elapsed_seconds(self) -> int:
        end = self.finished_at or datetime.now(UTC)
        return int((end - self.started_at).total_seconds())

    @property
    def universe_label(self) -> str:
        if self.symbols is None:
            return "historical S&P 500"
        n = len(self.symbols)
        return f"{n} symbol{'s' if n != 1 else ''}"


_LOCK = threading.Lock()
_CURRENT: BacktestJobState | None = None


def start_backtest(
    *,
    strategy: str,
    start: date | None = None,
    end: date | None = None,
    capital: float = DEFAULT_CAPITAL,
    slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
    commission: float = DEFAULT_COMMISSION,
    symbols: list[str] | None = None,
    note: str | None = None,
) -> tuple[BacktestJobState, bool]:
    """Start a background backtest, or report the one already running.

    Returns ``(state, started_new)``. ``started_new`` is False when an
    in-flight job exists — the caller should poll that one rather than
    report a fresh start. Date defaults match the CLI: ``end`` is today
    and ``start`` is five years before it. Raises ``KeyError`` for an
    unknown strategy before any thread is started.
    """
    global _CURRENT
    discover_strategies()
    STRATEGY_REGISTRY.get(strategy)
    end_d = end or date.today()
    start_d = start or end_d - timedelta(days=DEFAULT_WINDOW_DAYS)
    with _LOCK:
        if _CURRENT is not None and _CURRENT.status == "running":
            return _CURRENT, False
        _CURRENT = BacktestJobState(
            status="running",
            started_at=datetime.now(UTC),
            strategy=strategy,
            start=start_d,
            end=end_d,
            capital=capital,
            slippage_bps=slippage_bps,
            commission=commission,
            symbols=tuple(symbols) if symbols else None,
            note=note,
        )
        state = _CURRENT
    thread = threading.Thread(
        target=_execute, kwargs={"state": state}, daemon=True, name="backtest-run",
    )
    thread.start()
    return state, True


def current_job() -> BacktestJobState | None:
    """Snapshot of the current job (running or finished), or None."""
    with _LOCK:
        return _CURRENT


def consume_finished() -> BacktestJobState | None:
    """Pop the job if it has finished; None while running or when absent.

    The status endpoint calls this exactly once per completed job so the
    result line renders once and a later poll doesn't re-announce it.
    """
    global _CURRENT
    with _LOCK:
        if _CURRENT is None or _CURRENT.status == "running":
            return None
        finished, _CURRENT = _CURRENT, None
        return finished


def _set_finished(*, run_id: int | None, error: str | None) -> None:
    global _CURRENT
    with _LOCK:
        if _CURRENT is None:  # defensive: cleared concurrently
            return
        _CURRENT = replace(
            _CURRENT,
            status="error" if error else "done",
            finished_at=datetime.now(UTC),
            run_id=run_id,
            error=error,
        )


def _execute(*, state: BacktestJobState) -> None:
    """Thread target: run the engine, persist the run, record outcome."""
    log.info(
        "background backtest: %s on %s, %s → %s",
        state.strategy, state.universe_label, state.start, state.end,
    )
    try:
        run_id = _do_backtest(state)
    except Exception as exc:
        log.exception("background backtest: failed")
        _set_finished(run_id=None, error=f"Backtest failed: {exc}")
        return
    log.info("background backtest: saved run #%s", run_id)
    _set_finished(run_id=run_id, error=None)


def _do_backtest(state: BacktestJobState) -> int:
    """The actual work — build the CLI-equivalent config, run, save.

    Separated from :func:`_execute` so tests can monkeypatch this function
    and exercise the job lifecycle without bars or a database.
    """
    cfg = BacktestConfig(
        strategy_cls=STRATEGY_REGISTRY.get(state.strategy),
        start_date=state.start,
        end_date=state.end,
        starting_capital=Decimal(str(state.capital)),
        commission_per_trade=Decimal(str(state.commission)),
        slippage=FixedBpsSlippage(bps=Decimal(str(state.slippage_bps))),
        universe=list(state.symbols) if state.symbols else None,
    )
    result = BacktestEngine(cfg).run()
    return save_run(result, note=state.note)


def _reset_for_tests() -> None:
    """Clear job state between tests."""
    global _CURRENT
    with _LOCK:
        _CURRENT = None
