"""Signals query service — the single home for "read signals from the DB".

Extracted from ``web/routes/signals.py`` so that every caller — the web list
view, the POST /refresh re-render, and the MCP ``list_signals`` tool — runs the
*same* SELECT instead of each re-deriving the SQL. Consistent with the project
rule that a piece of logic has one home (principle #1).

The web route keeps its template-context shaping; this module owns only the
query itself (filters, version gate, sort, limit) and a single-signal lookup.
Rows are returned as SQLAlchemy ``Row`` objects so the existing templates that
access ``r.symbol`` / ``r.metadata`` keep working unchanged; the MCP layer
serializes them via ``r._mapping``.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from stockscan.db import session_scope
from stockscan.strategies import current_version_filter, discover_strategies

# Valid sort columns -> their SQL expressions. Lives here (not in the route)
# so the service owns the full query contract.
SORT_COLUMNS: dict[str, str] = {
    "symbol": "s.symbol",
    "strategy": "s.strategy_name",
    "score": "s.score",
    "entry": "s.suggested_entry",
    "stop": "s.suggested_stop",
    "qty": "s.suggested_qty",
    "date": "s.as_of_date",
    "side": "s.side",
}

_SELECT_COLUMNS = """
    s.signal_id, s.run_id, s.strategy_name, s.symbol, s.side,
    s.score, s.status, s.as_of_date,
    s.suggested_entry, s.suggested_stop, s.suggested_qty,
    s.rejected_reason, s.metadata
"""


def query_signals(
    *,
    strategy: str | None = None,
    days: int = 7,
    show_rejected: bool = True,
    symbol: str | None = None,
    side: str | None = None,
    score_min: float | None = None,
    score_max: float | None = None,
    sort: str | None = None,
    sort_dir: str | None = None,
    limit: int = 500,
    session: Session | None = None,
) -> list[Any]:
    """Return signal rows for the current strategy versions, newest first.

    Mirrors the web Signals list filters exactly:

    - ``days`` — only signals with ``as_of_date`` within the last N days.
    - ``strategy`` — restrict to one strategy name (None = all).
    - ``show_rejected`` — when False, only ``status='new'`` (passing) rows.
    - ``symbol`` — case-insensitive substring match on the ticker.
    - ``side`` — 'long' or 'short'.
    - ``score_min`` / ``score_max`` — inclusive score band.
    - ``sort`` / ``sort_dir`` — one of :data:`SORT_COLUMNS`, asc/desc.

    Always gated to the CURRENT registered version of each strategy, so retired
    versions never leak into the view. Returns SQLAlchemy ``Row`` objects.
    """
    discover_strategies()
    cutoff = date.today() - timedelta(days=days)

    where = ["s.as_of_date >= :d"]
    params: dict[str, object] = {"d": cutoff}
    if strategy:
        where.append("s.strategy_name = :strat")
        params["strat"] = strategy
    if not show_rejected:
        where.append("s.status = 'new'")
    if symbol:
        where.append("UPPER(s.symbol) LIKE UPPER(:sym_filter)")
        params["sym_filter"] = f"%{symbol}%"
    if side and side in ("long", "short"):
        where.append("s.side = :side_filter")
        params["side_filter"] = side
    if score_min is not None:
        where.append("s.score >= :score_min")
        params["score_min"] = score_min
    if score_max is not None:
        where.append("s.score <= :score_max")
        params["score_max"] = score_max

    # Restrict to the CURRENT registered version of each strategy.
    version_clause, version_params = current_version_filter(prefix="s")
    where.append(version_clause)
    params.update(version_params)

    sort_key = sort if sort in SORT_COLUMNS else None
    direction = "ASC" if sort_dir == "asc" else "DESC"
    if sort_key:
        order_by = f"{SORT_COLUMNS[sort_key]} {direction} NULLS LAST, s.signal_id DESC"
    else:
        order_by = "s.as_of_date DESC, s.status ASC, s.score DESC NULLS LAST"

    sql = text(
        f"""
        SELECT {_SELECT_COLUMNS}
        FROM signals s
        WHERE {" AND ".join(where)}
        ORDER BY {order_by}
        LIMIT :lim
        """
    )
    params["lim"] = limit

    def _run(s: Session) -> list[Any]:
        return s.execute(sql, params).all()

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)


def get_signal(signal_id: int, *, session: Session | None = None) -> Any | None:
    """Return a single signal row by id, or ``None`` if absent.

    Not version-gated — a direct lookup by primary key should resolve even a
    signal from a retired strategy version (e.g. following a link).
    """
    sql = text(
        f"""
        SELECT {_SELECT_COLUMNS}
        FROM signals s
        WHERE s.signal_id = :sid
        """
    )

    def _run(s: Session) -> Any | None:
        return s.execute(sql, {"sid": signal_id}).first()

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)
