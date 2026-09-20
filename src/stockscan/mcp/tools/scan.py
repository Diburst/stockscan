"""Scan + refresh tools. ``refresh_data`` starts the background pipeline; poll ``get_refresh_status``."""

from __future__ import annotations

from datetime import date
from typing import Any

from stockscan.mcp.serialize import jsonable
from stockscan.scan import ScanRunner
from stockscan.jobs import background
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


def refresh_data() -> dict[str, Any]:
    """Start the full refresh pipeline in the background. WRITE, async.

    The same run the Dashboard's Refresh button starts: bars + catch-up,
    FRED macro, regime, sector and watchlist composites, strategy scans
    (skipped when nothing is new), paper-trade upkeep, tonight's options
    book + settlement, the feeds the data plan allows, watchlist alerts.
    Single-flight: if a run is already in flight this joins it. Poll
    ``get_refresh_status`` for the current step and the final result.

    Returns:
        {"ok", "started_new", "status", "step", "started_at", "elapsed_seconds"}.
    """
    state, started_new = background.start()
    return {
        "ok": True,
        "started_new": started_new,
        "status": state.status,
        "step": f"{state.step_index}/{state.step_total} {state.step}",
        "started_at": state.started_at.isoformat(),
        "elapsed_seconds": state.elapsed_seconds,
        "note": "Poll get_refresh_status for progress and the final result.",
    }


def get_refresh_status() -> dict[str, Any]:
    """Check the current/most-recent refresh run.

    Returns:
        {"status": "idle" | "running" | "done" | "error", ...}. While
        running, ``step`` names the step in progress; when done, ``result``
        carries the counts (bars, scans, trades, options, feeds, alerts,
        step_failures).
    """
    job = background.current()
    if job is None:
        return {"status": "idle", "job": None}
    r = job.result
    return {
        "status": job.status,
        "step": f"{job.step_index}/{job.step_total} {job.step}",
        "started_at": job.started_at.isoformat(),
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "elapsed_seconds": job.elapsed_seconds,
        "result": None if r is None else {
            "as_of": r.as_of.isoformat(),
            "bars_upserted": r.bars_upserted,
            "caught_up": list(r.caught_up),
            "scans_skipped": r.scans_skipped,
            "scans": [
                {"strategy": s.strategy_name, "version": s.strategy_version,
                 "signals_emitted": s.signals_emitted, "rejected_count": s.rejected_count}
                for s in r.scans
            ],
            "trades_marked": r.trades_marked,
            "trades_auto_closed": r.trades_auto_closed,
            "options_run_id": r.options_run_id,
            "options_settled": r.options_settled,
            "feeds": r.feeds,
            "watchlist_alerts_fired": r.watchlist_alerts_fired,
            "step_failures": r.step_failures,
            "duration_seconds": round(r.duration_seconds, 1),
        },
        "error": job.error,
    }
