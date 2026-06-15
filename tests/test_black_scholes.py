"""Tests for the Black-Scholes pricing module + strike-from-delta solver.

Covers:
  * Closed-form pricing against textbook reference values.
  * Put-call parity: C − P = S − K·e^(−rT).
  * Greeks signs + bounds (call delta ∈ (0,1), put delta ∈ (−1,0),
    theta decay negative, vega positive and equal call/put).
  * The analytic strike-from-delta solver actually recovers the
    requested delta when the strike is fed back through ``greeks`` —
    this is the property the whole feature rests on.
  * ``suggest_strike`` ergonomics: sign normalisation from ``kind``,
    OTM direction (call above spot, put below), input guards.
"""

from __future__ import annotations

import math

import pytest

from stockscan.analysis import black_scholes as bs

# A reusable, well-behaved scenario: S=100, 30 days, 4% rate, 30% vol.
S = 100.0
T = 30.0 / 365.0
R = 0.04
SIGMA = 0.30


def test_put_call_parity():
    for k in (80.0, 95.0, 100.0, 110.0, 130.0):
        c = bs.price(S, k, T, R, SIGMA, "call")
        p = bs.price(S, k, T, R, SIGMA, "put")
        lhs = c - p
        rhs = S - k * math.exp(-R * T)
        assert lhs == pytest.approx(rhs, abs=1e-9)


def test_price_reference_value():
    # Reference: S=100, K=100, T=1y, r=5%, sigma=20% → call ≈ 10.4506.
    c = bs.price(100.0, 100.0, 1.0, 0.05, 0.20, "call")
    assert c == pytest.approx(10.4506, abs=1e-3)
    p = bs.price(100.0, 100.0, 1.0, 0.05, 0.20, "put")
    assert p == pytest.approx(5.5735, abs=1e-3)


def test_greeks_signs_and_bounds():
    gc = bs.greeks(S, 105.0, T, R, SIGMA, "call")
    gp = bs.greeks(S, 95.0, T, R, SIGMA, "put")
    assert 0.0 < gc.delta < 1.0
    assert -1.0 < gp.delta < 0.0
    assert gc.gamma > 0 and gp.gamma > 0
    assert gc.theta < 0  # long options bleed time value
    # Vega is identical for a call and a put at the same strike.
    same = bs.greeks(S, 100.0, T, R, SIGMA, "call")
    samep = bs.greeks(S, 100.0, T, R, SIGMA, "put")
    assert same.vega == pytest.approx(samep.vega, abs=1e-9)
    assert same.vega > 0


@pytest.mark.parametrize("kind,target", [("call", 0.20), ("put", -0.20),
                                         ("call", 0.35), ("put", -0.10)])
def test_strike_for_delta_recovers_target(kind, target):
    k = bs.strike_for_delta(S, T, R, SIGMA, target, kind)
    g = bs.greeks(S, k, T, R, SIGMA, kind)
    assert g.delta == pytest.approx(target, abs=1e-6)


def test_strike_otm_direction():
    # A 20-delta call sits above spot; a 20-delta put sits below spot.
    call = bs.suggest_strike(spot=S, vol_pct=30.0, days_to_expiry=30,
                             target_delta=0.20, kind="call", rate=R)
    put = bs.suggest_strike(spot=S, vol_pct=30.0, days_to_expiry=30,
                            target_delta=0.20, kind="put", rate=R)
    assert call.strike > S and call.pct_otm > 0
    assert put.strike < S and put.pct_otm < 0
    assert call.delta == pytest.approx(0.20, abs=1e-4)
    assert put.delta == pytest.approx(-0.20, abs=1e-4)
    # suggest_strike normalises the sign from kind even if a magnitude
    # is passed for the put.
    assert put.target_delta == pytest.approx(-0.20)


def test_suggest_strike_sign_normalisation():
    # Passing a positive magnitude for a put still yields the -delta put.
    put = bs.suggest_strike(spot=S, vol_pct=25.0, days_to_expiry=30,
                            target_delta=0.20, kind="put")
    assert put.target_delta < 0
    assert put.strike < S


def test_higher_vol_widens_strikes():
    lo = bs.suggest_strike(spot=S, vol_pct=15.0, days_to_expiry=30,
                           target_delta=0.20, kind="call", rate=R)
    hi = bs.suggest_strike(spot=S, vol_pct=45.0, days_to_expiry=30,
                           target_delta=0.20, kind="call", rate=R)
    # More vol → the same delta sits further OTM.
    assert hi.strike > lo.strike


@pytest.mark.parametrize("bad", [
    dict(spot=0.0, vol_pct=30.0, days_to_expiry=30, target_delta=0.2, kind="call"),
    dict(spot=100.0, vol_pct=0.0, days_to_expiry=30, target_delta=0.2, kind="call"),
    dict(spot=100.0, vol_pct=30.0, days_to_expiry=0, target_delta=0.2, kind="call"),
])
def test_suggest_strike_input_guards(bad):
    with pytest.raises(ValueError):
        bs.suggest_strike(**bad)


def test_strike_for_delta_rejects_out_of_range():
    with pytest.raises(ValueError):
        bs.strike_for_delta(S, T, R, SIGMA, 1.5, "call")
    with pytest.raises(ValueError):
        bs.strike_for_delta(S, T, R, SIGMA, 0.2, "put")  # put delta must be negative
