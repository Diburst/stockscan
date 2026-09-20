"""Dataclasses for the options-proposal engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

# Side identifiers — short premium only.
SELL_PUT = "sell_put"
SELL_CALL = "sell_call"


@dataclass(frozen=True, slots=True)
class OptionProposal:
    """One proposed short-premium trade (one side of one name, nearest expiry).

    ``rank_key`` (|move_sigma| × trend alignment) is the only ranking input.
    ``size_weight`` is the book multiplier the regime layer set for every row;
    ``contracts`` is this row's size against live equity. ``score_breakdown``
    holds the rank inputs so the page and the agent can explain the order.
    """

    symbol: str
    side: str  # SELL_PUT | SELL_CALL
    expiry_date: date | None
    days_to_expiry: int
    strike: float
    delta: float
    est_credit: float  # BS fair value per share (× 100 = per contract)
    pct_otm: float
    hv_pct: float  # annualised realized vol the leg was priced off
    hv_percentile: float | None  # 21d vol ranked in its trailing 252 (0–100)

    # Rank.
    move_sigma: float  # today's move in daily-σ units (residual for puts, raw for calls)
    trend_align: float
    rank_key: float

    # Size.
    size_weight: float  # book multiplier (identical per row)
    contracts: int | None

    # What a seller reads.
    sigma_distance: float  # strike distance in σ of the tenor's expected move
    credit_yield_ann: float  # est_credit / strike, annualised, in %

    # Context that drove the proposal.
    day_move_pct: float | None
    day_move_residual_pct: float | None
    days_to_earnings: int | None
    earnings_known: bool
    trend_bucket: str
    confluences: tuple[str, ...]  # displayed fact only; not ranked
    rationale: str
    score_breakdown: dict[str, Any] = field(default_factory=dict)
