"""Run the refresh pipeline on a background thread, single-flight.

The Dashboard's Refresh button and the MCP ``refresh_data`` tool start the
pipeline here and poll its state; the pipeline itself is
:func:`stockscan.jobs.pipeline.run_pipeline`.

One job at a time: a start while a job is running returns that job rather
than launching another, so an impatient double-click, a second browser
tab and an MCP call all watch the same run. State is module-global, which
is correct for the single-worker deployment this app uses (DEPLOY.md); a
multi-worker deployment would move it to a DB row.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Literal

from stockscan.jobs.pipeline import STEPS, PipelineResult, run_pipeline

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class JobState:
    status: Literal["running", "done", "error"]
    started_at: datetime
    step: str = STEPS[0]
    step_index: int = 1
    step_total: int = len(STEPS)
    finished_at: datetime | None = None
    result: PipelineResult | None = None
    error: str | None = None

    @property
    def running(self) -> bool:
        return self.status == "running"

    @property
    def elapsed_seconds(self) -> int:
        end = self.finished_at or datetime.now(UTC)
        return int((end - self.started_at).total_seconds())


_LOCK = threading.Lock()
_CURRENT: JobState | None = None


def start() -> tuple[JobState, bool]:
    """Start a run, or join the one in flight. Returns ``(state, started_new)``."""
    global _CURRENT
    with _LOCK:
        if _CURRENT is not None and _CURRENT.running:
            return _CURRENT, False
        _CURRENT = JobState(status="running", started_at=datetime.now(UTC))
        state = _CURRENT
    threading.Thread(target=_execute, daemon=True, name="refresh-pipeline").start()
    return state, True


def current() -> JobState | None:
    """The running or most recently finished job, or None."""
    with _LOCK:
        return _CURRENT


def _on_step(label: str, index: int, total: int) -> None:
    global _CURRENT
    with _LOCK:
        if _CURRENT is not None and _CURRENT.running:
            _CURRENT = replace(_CURRENT, step=label, step_index=index, step_total=total)


def _finish(*, result: PipelineResult | None, error: str | None) -> None:
    global _CURRENT
    with _LOCK:
        if _CURRENT is None:
            return
        _CURRENT = replace(
            _CURRENT,
            status="error" if error else "done",
            finished_at=datetime.now(UTC),
            result=result,
            error=error,
        )


def _execute() -> None:
    try:
        result = run_pipeline(send_summary=False, progress=_on_step)
    except Exception as exc:
        log.exception("background refresh failed")
        _finish(result=None, error=str(exc))
        return
    _finish(result=result, error=None)


def _reset_for_tests() -> None:
    global _CURRENT
    with _LOCK:
        _CURRENT = None
