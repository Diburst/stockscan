"""Position-level greeks + hedge share target added for the delta-hedge feature."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from stockscan.analysis import black_scholes as bs


def _t(days: float) -> float:
    return days / 365.0


def test_short_call_and_long_put_both_hedge_long_but_gamma_flips():
    # Both are negative-delta positions → hedged with LONG stock (target > 0).
    sc = bs.position_delta(1300, 1300, _t(30), 0.04, 0.30, "call", "short", 1)
    lp = bs.position_delta(1300, 1300, _t(30), 0.04, 0.30, "put", "long", 1)
    assert sc < 0 and lp < 0
    assert bs.hedge_target_shares(1300, 1300, _t(30), 0.04, 0.30, "call", "short", 1) > 0
    assert bs.hedge_target_shares(1300, 1300, _t(30), 0.04, 0.30, "put", "long", 1) > 0

    # Short call is short gamma (negative); long put is long gamma (positive).
    assert bs.position_gamma(1300, 1300, _t(30), 0.04, 0.30, "call", "short", 1) < 0
    assert bs.position_gamma(1300, 1300, _t(30), 0.04, 0.30, "put", "long", 1) > 0


def test_short_call_target_marches_0_to_100_across_the_strike():
    otm = bs.hedge_target_shares(1100, 1300, _t(30), 0.04, 0.30, "call", "short", 1)
    atm = bs.hedge_target_shares(1300, 1300, _t(30), 0.04, 0.30, "call", "short", 1)
    itm = bs.hedge_target_shares(1600, 1300, _t(30), 0.04, 0.30, "call", "short", 1)
    assert otm < 20  # far OTM → almost no hedge
    assert 30 <= atm <= 70  # ~half a contract ATM
    assert itm > 90  # deep ITM → ~full 100 shares/contract
    assert otm < atm < itm


def test_target_scales_with_contracts():
    one = bs.hedge_target_shares(1600, 1300, _t(30), 0.04, 0.30, "call", "short", 1)
    three = bs.hedge_target_shares(1600, 1300, _t(30), 0.04, 0.30, "call", "short", 3)
    assert three == pytest.approx(3 * one, abs=2)


def test_position_delta_sign_for_long_call_short_put_is_positive():
    # These hedge with SHORT stock (target < 0) — the general case the daemon
    # must also handle even though the user's examples are short call / long put.
    assert bs.position_delta(1300, 1300, _t(30), 0.04, 0.30, "call", "long", 1) > 0
    assert bs.hedge_target_shares(1300, 1300, _t(30), 0.04, 0.30, "put", "short", 1) < 0


def test_years_to_expiry_positive_and_floored():
    now = datetime(2026, 7, 1, tzinfo=UTC)
    assert bs.years_to_expiry(now + timedelta(days=365), now) == pytest.approx(1.0, abs=1e-6)
    # At/after expiry it floors to a tiny positive t (never zero → no div-by-zero).
    assert bs.years_to_expiry(now, now) > 0
    assert bs.years_to_expiry(now - timedelta(days=5), now) > 0


def test_invalid_side_raises():
    with pytest.raises(ValueError):
        bs.position_delta(1300, 1300, _t(30), 0.04, 0.30, "call", "sideways", 1)
