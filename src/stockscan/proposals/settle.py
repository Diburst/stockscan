"""Settle expired option proposals against the bars that followed them.

Phase 1 of the option tuning plan: nothing about the proposal engine can be
tuned until each proposal is scored against what the underlying actually did
through expiry. The nightly job calls :func:`settle_expired` after saving the
night's run; it fills the outcome columns on ``option_proposals`` from daily
bars alone — no chain data needed.

For every proposal whose ``expiry_date`` is before ``as_of`` and which has no
``settled_at`` yet, the bars from the run's ``as_of`` (exclusive) through the
expiry (inclusive) decide:

* ``touched`` — the strike traded intraday: any ``low ≤ strike`` for a
  short put, any ``high ≥ strike`` for a short call.
* ``breached`` — the strike was crossed on a close (same test on ``close``);
  ``breach_date`` is the first such close.
* ``close_at_expiry`` — the last close at-or-before the expiry date. A
  Friday can be a holiday, so the last bar on file inside the window counts.
* ``max_adverse_pct`` — the worst excursion relative to the strike in the
  direction that hurts the seller, in % of the strike, signed:
  ``(min_low − strike) / strike × 100`` for a short put and
  ``(strike − max_high) / strike × 100`` for a short call. Negative means the
  underlying went through the strike by that much; positive means it never
  did and this is how close it came.

A proposal with no bars in its window is left unsettled (the bars may
still be backfilled) and reported once in the log. ``settled_at`` makes the
step idempotent: a second run on the same night settles nothing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd
from sqlalchemy import text
from sqlalchemy.orm import Session

from stockscan.data.store import get_bars
from stockscan.db import session_scope

log = logging.getLogger(__name__)

_EXPIRED_UNSETTLED = text(
    """
    SELECT p.proposal_id, p.symbol, p.side, p.strike, p.expiry_date, r.as_of
    FROM option_proposals p
    JOIN option_proposal_runs r USING (run_id)
    WHERE p.expiry_date < :as_of AND p.settled_at IS NULL
    ORDER BY p.expiry_date, p.proposal_id
    """
)

_SETTLE = text(
    """
    UPDATE option_proposals
    SET touched = :touched,
        breached = :breached,
        breach_date = :breach_date,
        close_at_expiry = :close_at_expiry,
        max_adverse_pct = :max_adverse_pct,
        settled_at = NOW()
    WHERE proposal_id = :proposal_id
    """
)


@dataclass(frozen=True, slots=True)
class SettleResult:
    settled: int
    breached: int


@dataclass(frozen=True, slots=True)
class _Outcome:
    touched: bool
    breached: bool
    breach_date: date | None
    close_at_expiry: float
    max_adverse_pct: float


def settle_expired(as_of: date, *, session: Session | None = None) -> SettleResult:
    """Fill the outcome columns of every proposal that expired before ``as_of``."""
    if session is not None:
        return _settle(as_of, session)
    with session_scope() as s:
        return _settle(as_of, s)


def _settle(as_of: date, session: Session) -> SettleResult:
    rows = session.execute(_EXPIRED_UNSETTLED, {"as_of": as_of}).all()
    settled = breached = 0
    no_bars: list[str] = []
    for row in rows:
        expiry: date = row.expiry_date
        # Strikes are quoted in the dollars of the proposal day, so compare
        # them against unadjusted bars.
        bars = get_bars(
            row.symbol, row.as_of + timedelta(days=1), expiry, session=session, adjust=False
        )
        if bars.empty:
            no_bars.append(f"{row.symbol} {row.side} {expiry}")
            continue
        outcome = _outcome(row.side, float(row.strike), bars)
        session.execute(
            _SETTLE,
            {
                "proposal_id": row.proposal_id,
                "touched": outcome.touched,
                "breached": outcome.breached,
                "breach_date": outcome.breach_date,
                "close_at_expiry": outcome.close_at_expiry,
                "max_adverse_pct": outcome.max_adverse_pct,
            },
        )
        settled += 1
        breached += int(outcome.breached)
    if no_bars:
        log.warning(
            "settle: %d proposal(s) left unsettled, no bars through expiry: %s",
            len(no_bars), ", ".join(no_bars),
        )
    return SettleResult(settled=settled, breached=breached)


def _outcome(side: str, strike: float, bars: pd.DataFrame) -> _Outcome:
    """Score one proposal against the bars between its run date and expiry."""
    closes = bars["close"].astype(float)
    if side == "sell_put":
        low = bars["low"].astype(float)
        touched = bool((low <= strike).any())
        crossed = closes <= strike
        max_adverse_pct = (float(low.min()) - strike) / strike * 100.0
    else:
        high = bars["high"].astype(float)
        touched = bool((high >= strike).any())
        crossed = closes >= strike
        max_adverse_pct = (strike - float(high.max())) / strike * 100.0
    breached = bool(crossed.any())
    breach_date = crossed.idxmax().date() if breached else None
    return _Outcome(
        touched=touched,
        breached=breached,
        breach_date=breach_date,
        close_at_expiry=float(closes.iloc[-1]),
        max_adverse_pct=max_adverse_pct,
    )
