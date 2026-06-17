"""Scan + refresh tools. ``refresh_data`` is fire-and-poll."""

from __future__ import annotations

from datetime import date
from typing import Any

from stockscan.mcp.serialize import jsonable
from stockscan.scan import ScanRunner
from stockscan.scan.refresh_job import current_job, start_refresh
from stockscan.strategies import STRATEGY_REGISTRY, discover_strategies


def _parse_as_of(as_of: str | None) -> date | None:
    return date.fromisoformat(as_of) if as_of else None


def run_scan(
    strategy: str | None = None,
    all_strategies: bool = False,
    as_of: str | None = None,
) -> dict[str, Any]:
    """Run a strategy (or all strategies) and persist the resulting signals. WRITE.

    Args:
        strategy: Strategy name to run. Ignored when all_strategies is True.
        all_strategies: Run every registered strategy.
        as_of: ISO date (YYYY-MM-DD) to scan as of; default today.

    Returns:
        {"results": [{strategy, run_id, universe_size, signals_emitted,
        rejected_count} | {strategy, error}, ...]}.
    """
    discover_strategies()
    if all_strategies:
        targets = STRATEGY_REGISTRY.names()
    elif strategy:
        if strategy not in STRATEGY_REGISTRY.names():
            return {
                "error": "unknown_strategy",
                "name": strategy,
                "known": STRATEGY_REGISTRY.names(),
            }
        targets = [strategy]
    else:
        return {"error": "must_specify", "detail": "Pass strategy or all_strategies=true."}

    as_of_d = _parse_as_of(as_of)
    runner = ScanRunner()
    results: list[dict[str, Any]] = []
    for name in targets:
        try:
            summary = runner.run(name, as_of_d)
            results.append({"strategy": name, **jsonable(summary)})
        except Exception as exc:  # report per-strategy, keep going
            results.append({"strategy": name, "error": str(exc)})
    return {"results": results}


def refresh_data(days_back: int = 7) -> dict[str, Any]:
    """Start a background data refresh (bars + re-run strategies). WRITE, async.

    Fire-and-poll: this returns immediately. The refresh is single-flight — if
    one is already running, this joins it rather than starting a second. Poll
    ``get_refresh_status`` to see progress and the final summary.

    Args:
        days_back: How many days of bars to backfill before re-running (default 7).

    Returns:
        {"ok", "started_new", "status", "started_at", "elapsed_seconds"}.
    """
    state, started_new = start_refresh(days_back=days_back)
    return {
        "ok": True,
        "started_new": started_new,
        "status": state.status,
        "started_at": state.started_at.isoformat(),
        "elapsed_seconds": state.elapsed_seconds,
        "note": "Poll get_refresh_status for progress and the final summary.",
    }


def get_refresh_status() -> dict[str, Any]:
    """Check the status of the current/most-recent data refresh.

    Returns:
        {"status": "idle" | "running" | "done" | "error", ...}. When done,
        includes the refresh ``summary`` (bars upserted, signals emitted, etc.).
    """
    job = current_job()
    if job is None:
        return {"status": "idle", "job": None}
    return {
        "status": job.status,
        "started_at": job.started_at.isoformat(),
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "elapsed_seconds": job.elapsed_seconds,
        "summary": jsonable(job.summary),
        "error": job.error,
    }
