"""Backtest read tools: list past runs and export a run's results."""

from __future__ import annotations

from typing import Any

from stockscan.backtest.export import export_run
from stockscan.backtest.store import list_runs
from stockscan.mcp.serialize import jsonable


def list_backtests(strategy: str | None = None, limit: int = 25) -> dict[str, Any]:
    """List recent backtest runs, newest first.

    Args:
        strategy: Restrict to one strategy name; None = all.
        limit: Max runs to return (default 25).

    Returns:
        {"count", "runs": [{run_id, strategy_name, strategy_version, start_date,
        end_date, ...}, ...]}.
    """
    runs = list_runs(strategy, limit=limit)
    return {"count": len(runs), "runs": [jsonable(r) for r in runs]}


def get_backtest(
    run_id: int, include_per_day: bool = False, include_regime: bool = False
) -> dict[str, Any]:
    """Export a single backtest run's results (trades, score breakdowns, equity).

    Args:
        run_id: The run id (from list_backtests).
        include_per_day: Include the daily equity curve (larger payload).
        include_regime: Include the daily regime overlay across the window.

    Returns:
        The export bundle as a dict, or {"error": "not_found"} for an unknown id.
    """
    try:
        bundle = export_run(
            run_id, include_per_day=include_per_day, include_regime=include_regime
        )
    except (KeyError, ValueError) as exc:
        return {"error": "not_found", "run_id": run_id, "detail": str(exc)}
    if bundle is None:
        return {"error": "not_found", "run_id": run_id}
    return jsonable(bundle)
