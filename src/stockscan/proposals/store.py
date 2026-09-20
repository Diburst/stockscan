"""Persistence for proposal runs (opt-in via `stockscan options propose --save`).

Raw-SQL upserts in the project house style. Requires migration 0022; the
compute-on-demand paths (MCP tool, /options page) don't touch these tables.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from stockscan.db import session_scope
from stockscan.proposals.service import ProposalRun

_INSERT_RUN = text(
    """
    INSERT INTO option_proposal_runs
        (as_of, list_id, regime_label, vol_scalar, candidates, book_size)
    VALUES (:as_of, :list_id, :regime_label, :vol_scalar, :candidates, :book_size)
    RETURNING run_id
    """
)

_INSERT_PROPOSAL = text(
    """
    INSERT INTO option_proposals
        (run_id, rank, symbol, side, expiry_date, days_to_expiry, strike, delta,
         est_credit, pct_otm, iv_pct, score, size_weight, day_move_pct,
         days_to_earnings, confluence_count, trend_bucket,
         rationale, score_breakdown)
    VALUES
        (:run_id, :rank, :symbol, :side, :expiry, :dte, :strike, :delta,
         :credit, :pct_otm, :iv, :score, :size, :day_move,
         :dte_earn, :confl, :trend,
         :rationale, CAST(:breakdown AS JSONB))
    """
)


def save_run(run: ProposalRun, list_id: int | None = None, *, session: Session | None = None) -> int:
    """Persist a proposal run + its book. Returns the new run_id."""

    def _run(s: Session) -> int:
        reg = run.regime
        run_id = int(
            s.execute(
                _INSERT_RUN,
                {
                    "as_of": run.as_of,
                    "list_id": list_id,
                    "regime_label": reg.regime if reg is not None else None,
                    "vol_scalar": reg.vol_multiplier if reg is not None else None,
                    "candidates": run.candidates,
                    "book_size": len(run.book),
                },
            ).scalar_one()
        )
        for rank, p in enumerate(run.book, start=1):
            s.execute(
                _INSERT_PROPOSAL,
                {
                    "run_id": run_id, "rank": rank, "symbol": p.symbol, "side": p.side,
                    "expiry": p.expiry_date, "dte": p.days_to_expiry, "strike": p.strike,
                    "delta": p.delta, "credit": p.est_credit, "pct_otm": p.pct_otm,
                    "iv": p.iv_pct, "score": p.score, "size": p.size_weight,
                    "day_move": p.day_move_pct, "dte_earn": p.days_to_earnings,
                    "confl": p.confluence_count,
                    "trend": p.trend_bucket, "rationale": p.rationale,
                    "breakdown": json.dumps(p.score_breakdown),
                },
            )
        return run_id

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)


def latest_run(*, session: Session | None = None) -> dict[str, Any] | None:
    """The most recent proposal run header, or None."""
    sql = text(
        "SELECT run_id, as_of, regime_label, vol_scalar, candidates, book_size, "
        "created_at FROM option_proposal_runs ORDER BY created_at DESC LIMIT 1"
    )

    def _run(s: Session) -> dict[str, Any] | None:
        row = s.execute(sql).first()
        return dict(row._mapping) if row is not None else None

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)
