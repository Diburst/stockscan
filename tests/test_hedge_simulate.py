"""The offline hedge simulator: paths, single-run engine, Monte Carlo, sweep."""

from __future__ import annotations

import pytest

from stockscan.hedge import simulate as sim
from stockscan.hedge.policy import HedgePolicy


def _spec(**kw):
    base = dict(option_kind="call", option_side="short", spot=1300, dte=30,
                iv_pct=45, rate_pct=4, contracts=1, target_delta=0.30)
    base.update(kw)
    return sim.build_option_spec(**base)


# ---- paths ----
def test_gbm_path_is_deterministic_and_right_length():
    a = sim.gbm_path(s0=1300, annual_vol_pct=40, days=30, steps_per_day=20, seed=7)
    b = sim.gbm_path(s0=1300, annual_vol_pct=40, days=30, steps_per_day=20, seed=7)
    c = sim.gbm_path(s0=1300, annual_vol_pct=40, days=30, steps_per_day=20, seed=8)
    assert a.spots == b.spots
    assert a.spots != c.spots
    assert len(a) == 30 * 20 + 1
    assert a.spots[0] == 1300


def test_flat_path_trades_far_less_than_a_volatile_one():
    spec = _spec()
    flat = sim.gbm_path(s0=1300, annual_vol_pct=0.0, drift_pct=0.0, days=30, steps_per_day=20, seed=1)
    wild = sim.gbm_path(s0=1300, annual_vol_pct=70.0, drift_pct=0.0, days=30, steps_per_day=20, seed=1)
    n_flat = sim.simulate_hedge(spec, HedgePolicy(), flat).summary["num_trades"]
    n_wild = sim.simulate_hedge(spec, HedgePolicy(), wild).summary["num_trades"]
    # A flat price only retrades on slow theta drift; a volatile one churns the band.
    assert n_flat < n_wild


# ---- build_option_spec ----
def test_build_option_spec_solves_strike_and_premium():
    spec = _spec()  # 30-delta short call
    assert spec.strike > 1300  # OTM call above spot
    assert spec.premium > 0


# ---- single-run behaviour ----
def test_rising_path_short_call_accumulates_and_bleeds():
    # Deterministic monotonic rise straight through the strike → guaranteed deep
    # ITM, so the accumulation/settlement behaviour is testable without seed luck.
    from datetime import UTC, datetime, timedelta

    spec = _spec()  # 30-delta short call, strike ~1400
    start = datetime.now(UTC)
    n = 400
    spots = [1300 + (1650 - 1300) * i / (n - 1) for i in range(n)]
    # Span 25 days — inside the 30-day option life, so the ramp completes before
    # expiry and the path settles deep ITM at ~1650.
    times = [start + timedelta(days=25 * i / (n - 1)) for i in range(n)]
    path = sim.PricePath(times=times, spots=spots, label="ramp", source="synthetic")
    res = sim.simulate_hedge(spec, HedgePolicy(), path)
    s = res.summary
    assert res.series[-1]["held"] >= 90  # marched to ~100 shares/contract
    assert s["in_the_money"] is True
    assert s["option_pnl"] < 0  # short call deep ITM loses
    assert s["hedge_pnl"] > 0  # long stock hedge gained


def test_result_series_and_trades_are_populated():
    spec = _spec()
    path = sim.gbm_path(s0=1300, annual_vol_pct=50, days=30, steps_per_day=20, seed=5)
    res = sim.simulate_hedge(spec, HedgePolicy(), path)
    assert len(res.series) > 0
    assert res.sampled_series(50).__len__() <= len(res.series)
    assert all({"spot", "held", "net_pnl"} <= set(pt) for pt in res.series)


# ---- Monte Carlo ----
def test_monte_carlo_distribution_shape():
    spec = _spec()
    m = sim.monte_carlo(spec, HedgePolicy(), n_paths=40, s0=1300, annual_vol_pct=45,
                        days=30, steps_per_day=12, seed=0)
    assert m["n_paths"] == 40
    assert len(m["samples"]) == 40
    assert m["net_pnl"]["p5"] <= m["net_pnl"]["median"] <= m["net_pnl"]["p95"]
    assert 0.0 <= m["win_rate"] <= 1.0


# ---- sweep ----
def test_sweep_tighter_band_trades_at_least_as_much():
    spec = _spec()
    path = sim.gbm_path(s0=1300, annual_vol_pct=50, days=30, steps_per_day=12, seed=1)
    grid = sim.ww_risk_aversion_grid([0.001, 0.05, 1.0])  # increasing risk aversion ⇒ tighter
    sw = sim.sweep(spec, grid, path=path)
    trades = [r["num_trades"] for r in sw["rows"]]
    assert trades[0] <= trades[-1]  # loosest band trades no more than the tightest
    assert sw["mode"] == "single_path"
    assert sw["count"] == 3


def test_sweep_requires_exactly_one_of_path_or_mc():
    spec = _spec()
    grid = sim.ww_risk_aversion_grid([0.05])
    with pytest.raises(ValueError):
        sim.sweep(spec, grid)  # neither path nor mc


def test_resolve_spec_and_path_synthetic_requires_spot():
    with pytest.raises(ValueError):
        sim.resolve_spec_and_path()  # no symbol and no spot
