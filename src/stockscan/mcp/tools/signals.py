"""Signal-reading tools."""

from __future__ import annotations

from typing import Any

from stockscan.mcp.serialize import jsonable
from stockscan.signals import get_signal as _get_signal
from stockscan.signals import query_signals


def list_signals(
    strategy: str | None = None,
    days: int = 7,
    include_rejected: bool = True,
    symbol: str | None = None,
    side: str | None = None,
    score_min: float | None = None,
    score_max: float | None = None,
    sort: str | None = None,
    sort_dir: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """List recent trading signals for the current strategy versions.

    Returns passing and rejected signals from the last ``days`` days, newest
    first, gated to each strategy's currently-registered version.

    Args:
        strategy: Restrict to one strategy name (e.g. "reversal_swing"). None = all.
        days: Look-back window in days (default 7).
        include_rejected: Include rejected signals as well as passing ones.
        symbol: Case-insensitive ticker substring filter (e.g. "AAPL").
        side: "long" or "short".
        score_min: Inclusive lower bound on signal score.
        score_max: Inclusive upper bound on signal score.
        sort: One of symbol, strategy, score, entry, stop, qty, date, side.
        sort_dir: "asc" or "desc" (default desc).
        limit: Max rows to scan (default 200).

    Returns:
        {"count", "passing": [...], "rejected": [...]} where each item has the
        signal id, strategy, symbol, side, score, status, suggested entry/stop/
        qty, rejected reason, and metadata (incl. score breakdown).
    """
    rows = query_signals(
        strategy=strategy,
        days=days,
        show_rejected=include_rejected,
        symbol=symbol,
        side=side,
        score_min=score_min,
        score_max=score_max,
        sort=sort,
        sort_dir=sort_dir,
        limit=limit,
    )
    items = [jsonable(r) for r in rows]
    passing = [i for i in items if i.get("status") == "new"]
    rejected = [i for i in items if i.get("status") == "rejected"]
    return {"count": len(items), "passing": passing, "rejected": rejected}


def get_signal(signal_id: int) -> dict[str, Any]:
    """Fetch a single signal by its id, including the full score breakdown.

    Args:
        signal_id: The signal's primary-key id (from list_signals).

    Returns:
        The signal row as a dict, or {"error": "not_found"} if no such id.
    """
    row = _get_signal(signal_id)
    if row is None:
        return {"error": "not_found", "signal_id": signal_id}
    return jsonable(row)
