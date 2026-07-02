"""The pure per-tick decision (`plan_tick`) that drives the daemon."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from stockscan.hedge.daemon import plan_tick
from stockscan.hedge.policy import HedgePolicy

_NOW = datetime(2026, 7, 1, tzinfo=UTC)
_EXP = _NOW + timedelta(days=30)


def _plan(held: int, spot: float, *, policy: HedgePolicy | None = None, last_hedge_spot=None):
    return plan_tick(
        held_shares=held,
        option_kind="call",
        option_side="short",
        strike=1300.0,
        contracts=1,
        multiplier=100,
        iv_pct=30.0,
        rate_pct=4.0,
        expiry=_EXP,
        policy=policy or HedgePolicy(),
        spot=spot,
        last_hedge_spot=last_hedge_spot,
        now=_NOW,
    )


def test_plan_computes_positive_target_for_short_call():
    plan = _plan(0, 1300.0)
    assert not plan.expired
    assert plan.target_shares > 0  # long stock hedges a short call
    assert plan.rebalance  # from flat, far outside the band
    assert plan.fill_qty == plan.target_shares


def test_plan_flags_expiry():
    plan = plan_tick(
        held_shares=50, option_kind="call", option_side="short", strike=1300.0,
        contracts=1, multiplier=100, iv_pct=30.0, rate_pct=4.0,
        expiry=_NOW - timedelta(seconds=1), policy=HedgePolicy(),
        spot=1300.0, last_hedge_spot=1300.0, now=_NOW,
    )
    assert plan.expired
    assert not plan.rebalance


def test_plan_holds_when_already_at_target():
    # Compute the target, then feed it back as our holding → no trade.
    target = _plan(0, 1300.0).target_shares
    plan = _plan(target, 1300.0, last_hedge_spot=1300.0)
    assert plan.target_shares == target
    assert not plan.rebalance
    assert plan.fill_qty == 0


def test_plan_fill_qty_moves_toward_target():
    plan = _plan(10, 1600.0)  # deep ITM ⇒ target near 100, held only 10
    assert plan.fill_qty > 0
    assert plan.target_shares == 10 + plan.fill_qty
