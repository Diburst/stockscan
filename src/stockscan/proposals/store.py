"""Persistence for proposal runs.

Raw-SQL inserts in the project house style (migrations 0022 + 0027). The
nightly job saves one run per night and ``settle.py`` fills the outcome
columns after expiry; ``trigger_base_rates`` reads those back for the page.
The compute-on-demand paths (MCP tool, /options page) don't write here.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from stockscan.db import session_scope
from stockscan.proposals.service import ProposalRun

BASE_RATE_MIN_N = 30  # settled proposals a trigger class needs before its rate is shown

_INSERT_RUN = text(
    """
    INSERT INTO option_proposal_runs
        (as_of, list_id, regime_label, vol_scalar, trend_gate_open,
         credit_stress_flag, book_mult, macro_events, candidates, book_size)
    VALUES (:as_of, :list_id, :regime_label, :vol_scalar, :gate_open,
            :stress, :book_mult, :macro, :candidates, :book_size)
    RETURNING run_id
    """
)

_INSERT_PROPOSAL = text(
    """
    INSERT INTO option_proposals
        (run_id, rank, symbol, side, expiry_date, days_to_expiry, strike, delta,
         est_credit, pct_otm, hv_pct, move_sigma, rank_key, size_weight, contracts,
         sigma_distance, credit_yield_ann, day_move_pct, day_move_residual_pct,
         days_to_earnings, earnings_known, trend_bucket, rationale, score_breakdown)
    VALUES
        (:run_id, :rank, :symbol, :side, :expiry, :dte, :strike, :delta,
         :credit, :pct_otm, :hv, :move_sigma, :rank_key, :size, :contracts,
         :sigma_distance, :yield_ann, :day_move, :day_move_residual,
         :dte_earn, :earnings_known, :trend, :rationale, CAST(:breakdown AS JSONB))
    """
)

_DELETE_PROPOSALS_FOR_DATE = text(
    "DELETE FROM option_proposals WHERE run_id IN "
    "(SELECT run_id FROM option_proposal_runs WHERE as_of = :as_of "
    " AND list_id IS NOT DISTINCT FROM :list_id)"
)
_DELETE_RUNS_FOR_DATE = text(
    "DELETE FROM option_proposal_runs WHERE as_of = :as_of "
    "AND list_id IS NOT DISTINCT FROM :list_id"
)

_LATEST_RUN = text(
    "SELECT run_id, as_of, regime_label, vol_scalar, trend_gate_open, credit_stress_flag, "
    "book_mult, macro_events, candidates, book_size, created_at "
    "FROM option_proposal_runs ORDER BY created_at DESC LIMIT 1"
)

_BASE_RATES = text(
    """
    SELECT p.side, p.trend_bucket, r.trend_gate_open,
           COUNT(*) AS proposed,
           COUNT(*) FILTER (WHERE p.breached) AS breached
    FROM option_proposals p
    JOIN option_proposal_runs r ON r.run_id = p.run_id
    WHERE p.settled_at IS NOT NULL
    GROUP BY p.side, p.trend_bucket, r.trend_gate_open
    """
)


def save_run(
    run: ProposalRun,
    list_id: int | None = None,
    *,
    replace: bool = False,
    session: Session | None = None,
) -> int:
    """Persist a proposal run + its book. Returns the new run_id.

    ``replace`` drops any earlier run saved for the same as-of date and
    list first, so a day never carries two books into the base rates."""

    def _run(s: Session) -> int:
        reg = run.regime
        if replace:
            params = {"as_of": run.as_of, "list_id": list_id}
            s.execute(_DELETE_PROPOSALS_FOR_DATE, params)
            s.execute(_DELETE_RUNS_FOR_DATE, params)
        run_id = int(
            s.execute(
                _INSERT_RUN,
                {
                    "as_of": run.as_of,
                    "list_id": list_id,
                    "regime_label": reg.regime if reg is not None else None,
                    "vol_scalar": reg.vol_multiplier if reg is not None else None,
                    "gate_open": reg.trend_gate_open if reg is not None else None,
                    "stress": reg.credit_stress_flag if reg is not None else None,
                    "book_mult": run.book_mult,
                    "macro": " · ".join(run.macro_events) or None,
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
                    "hv": p.hv_pct, "move_sigma": p.move_sigma, "rank_key": p.rank_key,
                    "size": p.size_weight, "contracts": p.contracts,
                    "sigma_distance": p.sigma_distance, "yield_ann": p.credit_yield_ann,
                    "day_move": p.day_move_pct, "day_move_residual": p.day_move_residual_pct,
                    "dte_earn": p.days_to_earnings, "earnings_known": p.earnings_known,
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

    def _run(s: Session) -> dict[str, Any] | None:
        row = s.execute(_LATEST_RUN).first()
        return dict(row._mapping) if row is not None else None

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)


def trigger_base_rates(
    *, session: Session | None = None
) -> dict[tuple[str, str, bool | None], tuple[int, int]]:
    """``{(side, trend_bucket, gate_open): (proposed, breached)}`` over settled
    proposals — the base rate for each trigger class the page shows."""

    def _run(s: Session) -> dict[tuple[str, str, bool | None], tuple[int, int]]:
        return {
            (r.side, r.trend_bucket, r.trend_gate_open): (int(r.proposed), int(r.breached))
            for r in s.execute(_BASE_RATES)
        }

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)
