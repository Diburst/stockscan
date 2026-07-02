"""HedgePolicy — the no-transaction band that decides when to rehedge."""

from __future__ import annotations

from stockscan.hedge.policy import (
    FIXED_SHARES,
    PCT_MOVE,
    WHALLEY_WILMOTT,
    HedgePolicy,
)


def test_default_is_whalley_wilmott():
    assert HedgePolicy().mode == WHALLEY_WILMOTT


def test_ww_band_widens_with_gamma_and_tightens_with_risk_aversion():
    p_low_a = HedgePolicy(risk_aversion=0.001)
    p_high_a = HedgePolicy(risk_aversion=1.0)
    # More risk-averse ⇒ tighter band.
    assert p_high_a.band_shares(1300, 0.3) < p_low_a.band_shares(1300, 0.3)
    # More gamma ⇒ wider band.
    p = HedgePolicy()
    assert p.band_shares(1300, 0.6) > p.band_shares(1300, 0.2)


def test_ww_zero_gamma_falls_back_to_min_band():
    p = HedgePolicy(min_band_shares=2.0)
    assert p.band_shares(1300, 0.0) == 2.0


def test_should_rebalance_never_trades_zero_shares():
    p = HedgePolicy(mode=FIXED_SHARES, fixed_band_shares=3)
    assert not p.should_rebalance(
        held_shares=50, target_shares=50, spot=1300, position_gamma=0.3, last_hedge_spot=1290
    )


def test_fixed_band_triggers_outside_the_band_only():
    p = HedgePolicy(mode=FIXED_SHARES, fixed_band_shares=5)
    common = dict(spot=1300, position_gamma=0.3, last_hedge_spot=1300)
    assert not p.should_rebalance(held_shares=50, target_shares=54, **common)  # drift 4 ≤ 5
    assert p.should_rebalance(held_shares=50, target_shares=57, **common)  # drift 7 > 5


def test_pct_move_uses_spot_distance():
    p = HedgePolicy(mode=PCT_MOVE, pct_move=0.02)
    # Target differs, but spot only moved 0.8% ⇒ hold.
    assert not p.should_rebalance(
        held_shares=50, target_shares=55, spot=1300, position_gamma=0.3, last_hedge_spot=1290
    )
    # Spot moved > 2% ⇒ trade.
    assert p.should_rebalance(
        held_shares=50, target_shares=55, spot=1340, position_gamma=0.3, last_hedge_spot=1300
    )
    # No reference spot yet ⇒ establish the hedge.
    assert p.should_rebalance(
        held_shares=0, target_shares=50, spot=1300, position_gamma=0.3, last_hedge_spot=None
    )


def test_roundtrip_to_from_dict():
    p = HedgePolicy(mode=FIXED_SHARES, fixed_band_shares=7, risk_aversion=0.2)
    assert HedgePolicy.from_dict(p.to_dict()) == p
    # Unknown/extra keys are ignored; empty → defaults.
    assert HedgePolicy.from_dict({"mode": WHALLEY_WILMOTT, "bogus": 1}).mode == WHALLEY_WILMOTT
    assert HedgePolicy.from_dict(None) == HedgePolicy()


def test_bad_mode_raises():
    import pytest

    with pytest.raises(ValueError):
        HedgePolicy(mode="teleport")
