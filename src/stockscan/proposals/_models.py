"""Dataclasses for the options-proposal engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

# Side identifiers — short-premium only in v1.
SELL_PUT = "sell_put"
SELL_CALL = "sell_call"


@dataclass(frozen=True, slots=True)
class OptionProposal:
    """One proposed short-premium trade (one side of one name, nearest expiry).

    ``score`` is the 0–1 attractiveness used for ranking; ``size_weight`` is the
    regime-and-side-adjusted relative size (0–1) you scale your per-trade risk
    by. ``score_breakdown`` keeps the per-input contributions so the UI/agent can
    explain the rank, mirroring the signal score-derivation card.
    """

    symbol: str
    side: str  # SELL_PUT | SELL_CALL
    expiry_date: date | None
    days_to_expiry: int
    strike: float
    delta: float
    est_credit: float  # BS fair value per share (× 100 = per contract)
    pct_otm: float
    iv_pct: float

    score: float
    size_weight: float

    # Context that drove the proposal.
    day_move_pct: float | None
    days_to_earnings: int | None
    confluence_count: int
    pct_to_threat: float | None  # distance to the threatened level (R for call, S for put)
    trend_bucket: str
    rationale: str
    # Context flag (NOT scored): current price is itself sitting at the level
    # it's selling against — support for a put, resistance for a call. The
    # "price at confirmed level" timing signal, surfaced as a callout.
    price_at_level: bool = False
    score_breakdown: dict[str, Any] = field(default_factory=dict)
