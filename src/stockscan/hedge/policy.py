"""HedgePolicy — the no-transaction band that decides *when* to rehedge.

Reads like a book: open this file and you can see exactly what makes the daemon
buy or sell stock. Continuous hedging is impossible and would churn the account
to death on transaction costs, so we hold the stock position inside a band
around the frictionless delta hedge and only trade when we fall out of it.

Three band modes, one default
-----------------------------
  * ``whalley_wilmott`` (default) — the cost-optimal no-transaction band from
    Whalley & Wilmott (1997). Half-width

        w = ( 3·c·Γ²·S / (2·a) ) ^ (1/3)          [shares]

    where c is the round-trip transaction-cost rate, Γ is the position gamma
    (share-equivalents per $1 of spot), S is spot, and a is the trader's risk
    aversion. The band **auto-widens when gamma is low** (cheap to let the hedge
    drift) and **tightens near the strike** where gamma is high — which is
    exactly where an untended short-gamma position hurts. One knob that matters:
    ``a`` (bigger a → tighter band → more trades, less tracking error).

  * ``fixed_shares`` — rehedge when |held − target| ≥ N shares. Dead simple,
    fully predictable churn, ignores gamma.

  * ``pct_move`` — rehedge when spot has moved ≥ X% since the last hedge (and the
    target has actually changed). Cheap and intuitive, but blind to how much
    delta actually moved.

The policy is stored per-position as JSON in ``hedge_positions.band_policy`` so
each hedge can carry its own knobs; :meth:`to_dict` / :meth:`from_dict` round-trip
it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

# Modes.
WHALLEY_WILMOTT = "whalley_wilmott"
FIXED_SHARES = "fixed_shares"
PCT_MOVE = "pct_move"

_VALID_MODES = frozenset({WHALLEY_WILMOTT, FIXED_SHARES, PCT_MOVE})


@dataclass(frozen=True, slots=True)
class HedgePolicy:
    """When to rehedge. Defaults to the Whalley-Wilmott gamma-scaled band."""

    mode: str = WHALLEY_WILMOTT

    # Whalley-Wilmott knobs.
    cost_rate: float = 0.0005  # round-trip transaction cost as a fraction (5 bps).
    risk_aversion: float = 0.05  # `a` — bigger ⇒ tighter band ⇒ more trades.
    min_band_shares: float = 1.0  # never rebalance for a sub-share drift.
    max_band_shares: float = 1e9  # optional safety clamp.

    # fixed_shares knob.
    fixed_band_shares: float = 5.0

    # pct_move knob.
    pct_move: float = 0.01  # 1% spot move since last hedge.

    def __post_init__(self) -> None:
        if self.mode not in _VALID_MODES:
            raise ValueError(f"unknown hedge band mode {self.mode!r}")

    # ---- The band, in shares ----
    def band_shares(self, spot: float, position_gamma: float) -> float:
        """Half-width of the no-transaction band around the target, in shares.

        Only meaningful for ``whalley_wilmott`` and ``fixed_shares``; ``pct_move``
        uses a spot-distance trigger instead (see :meth:`should_rebalance`).
        """
        if self.mode == FIXED_SHARES:
            return max(self.min_band_shares, self.fixed_band_shares)

        if self.mode == WHALLEY_WILMOTT:
            gamma = abs(position_gamma)
            if gamma <= 0.0 or spot <= 0.0 or self.risk_aversion <= 0.0:
                # No gamma ⇒ no reason to trade until the target itself moves;
                # fall back to the min band so a stale drift still gets cleaned up.
                return self.min_band_shares
            w = (3.0 * self.cost_rate * gamma * gamma * spot / (2.0 * self.risk_aversion)) ** (1.0 / 3.0)
            return min(self.max_band_shares, max(self.min_band_shares, w))

        # pct_move: band isn't the trigger, but expose min for callers that read it.
        return self.min_band_shares

    # ---- The decision ----
    def should_rebalance(
        self,
        *,
        held_shares: int,
        target_shares: int,
        spot: float,
        position_gamma: float,
        last_hedge_spot: float | None,
    ) -> bool:
        """True when the stock position has drifted far enough to retrade.

        ``last_hedge_spot`` is the spot at which we last adjusted (or opened);
        only ``pct_move`` uses it. All modes require the integer target to
        actually differ from what we hold — we never place a zero-share order.
        """
        drift = abs(held_shares - target_shares)
        if drift == 0:
            return False

        if self.mode == PCT_MOVE:
            if last_hedge_spot is None or last_hedge_spot <= 0:
                return True  # no reference yet ⇒ establish the hedge.
            moved = abs(spot - last_hedge_spot) / last_hedge_spot
            return moved >= self.pct_move

        # whalley_wilmott / fixed_shares: compare drift to the band half-width.
        return drift > self.band_shares(spot, position_gamma)

    # ---- (de)serialisation for the band_policy JSONB column ----
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> HedgePolicy:
        if not data:
            return cls()
        allowed = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in allowed})
