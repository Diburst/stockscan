"""Background-job wrapper around :func:`stockscan.scan.refresh.refresh_signals`.

The Fetch Latest button used to run the refresh inside the request — tens of
seconds during which the response (and, before the sync-handler fix, the whole
app) hung. Now the POST starts the work on a daemon thread and returns
immediately; the Signals page polls ``GET /signals/refresh/status`` until the
job lands and then swaps in the refreshed content.

Design constraints, deliberately simple:

- **In-process, single-flight.** One job at a time, guarded by a lock; a
  second POST while running joins the in-flight job instead of double
  fetching. State lives in module globals — correct for the single
  uvicorn-worker deployment this app uses (documented in DEPLOY.md). If the
  app ever runs multi-worker, this moves to a DB row; don't paper over it
  with sticky sessions.
- **Own DB session.** The thread cannot reuse the request's session;
  it opens its own ``session_scope()`` so commit/rollback are self-contained.
- **Result shape matches the old synchronous path** — the ``summary`` dict
  feeds the same ``signals/_signals_content.html`` banner that the
  synchronous implementation rendered, so the template didn't have to change
  shape.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Literal

from stockscan.config import settings
from stockscan.data.providers.eodhd import EODHDError, EODHDProvider
from stockscan.db import session_scope
from stockscan.positions.paper_store import check_auto_close, mark_to_market
from stockscan.scan.refresh import refresh_signals

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RefreshJobState:
    """Immutable snapshot of the current/most-recent refresh job."""

    status: Literal["running", "done", "error"]
    started_at: datetime
    finished_at: datetime | None = None
    summary: dict[str, Any] | None = None  # template-ready banner payload
    error: str | None = None

    @property
    def elapsed_seconds(self) -> int:
        end = self.finished_at or datetime.now(UTC)
        return int((end - self.started_at).total_seconds())


_LOCK = threading.Lock()
_CURRENT: RefreshJobState | None = None


def start_refresh(*, days_back: int = 7) -> tuple[RefreshJobState, bool]:
    """Start a background refresh, or join the one already running.

    Returns ``(state, started_new)``. ``started_new`` is False when an
    in-flight job exists — the caller should poll that one rather than
    report a fresh start.
    """
    global _CURRENT
    with _LOCK:
        if _CURRENT is not None and _CURRENT.status == "running":
            return _CURRENT, False
        _CURRENT = RefreshJobState(status="running", started_at=datetime.now(UTC))
        state = _CURRENT
    thread = threading.Thread(
        target=_execute, kwargs={"days_back": days_back}, daemon=True,
        name="signals-refresh",
    )
    thread.start()
    return state, True


def current_job() -> RefreshJobState | None:
    """Snapshot of the current job (running or finished), or None."""
    with _LOCK:
        return _CURRENT


def consume_finished() -> RefreshJobState | None:
    """Pop the job if it has finished; None while running or when absent.

    The status endpoint calls this exactly once per completed job so the
    result banner renders once and a later poll doesn't re-announce it.
    """
    global _CURRENT
    with _LOCK:
        if _CURRENT is None or _CURRENT.status == "running":
            return None
        finished, _CURRENT = _CURRENT, None
        return finished


def _set_finished(*, summary: dict[str, Any] | None, error: str | None) -> None:
    global _CURRENT
    with _LOCK:
        if _CURRENT is None:  # defensive: cleared concurrently
            return
        _CURRENT = replace(
            _CURRENT,
            status="error" if error else "done",
            finished_at=datetime.now(UTC),
            summary=summary,
            error=error,
        )


def _execute(*, days_back: int) -> None:
    """Thread target: run the refresh in its own session, record outcome."""
    try:
        summary = _do_refresh(days_back=days_back)
    except EODHDError as exc:
        log.warning("background refresh: provider error: %s", exc)
        _set_finished(summary=None, error=f"Provider error: {exc}")
        return
    except Exception as exc:
        log.exception("background refresh: unexpected error")
        _set_finished(summary=None, error=f"Refresh failed: {exc}")
        return
    _set_finished(summary=summary, error=None)


def _do_refresh(*, days_back: int) -> dict[str, Any]:
    """The actual work — bars + strategy fan-out + paper-trade upkeep.

    Separated from :func:`_execute` so tests can monkeypatch this function
    and exercise the job lifecycle without a provider or database.
    """
    api_key = settings.eodhd_api_key.get_secret_value()
    if not api_key:
        raise EODHDError("EODHD_API_KEY is not set. Add it to your .env to fetch bars.")

    with session_scope() as s:
        with EODHDProvider(api_key=api_key) as provider:
            result = refresh_signals(provider, days_back=days_back, session=s)

        # Propagate fresh bar data to paper trades: update P/L then
        # auto-close any that hit stop/target/time exits.
        trades_marked = mark_to_market(session=s)
        trades_auto_closed = check_auto_close(session=s)

    return {
        "up_to_date": result.up_to_date,
        "bars_upserted": result.bars_upserted,
        "bars_days_covered": result.bars_days_covered,
        "bars_failed_days": [str(d) for d in result.bars_failed_days],
        "strategies_run": result.strategies_run,
        "signals_emitted": result.signals_emitted,
        "rejected_count": result.rejected_count,
        "failures": [
            {"strategy_name": f.strategy_name, "error": f.error}
            for f in result.failures
        ],
        "duration_seconds": round(result.duration_seconds, 1),
        "trades_marked": trades_marked,
        "trades_auto_closed": len(trades_auto_closed),
    }


def _reset_for_tests() -> None:
    """Clear job state between tests."""
    global _CURRENT
    with _LOCK:
        _CURRENT = None
