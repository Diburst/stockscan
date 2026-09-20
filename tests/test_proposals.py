"""Unit tests for the options-proposal engine (pure; fake SymbolAnalysis)."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from math import floor, sqrt
from types import SimpleNamespace

import pytest

from stockscan.proposals import service
from stockscan.proposals._models import SELL_CALL, SELL_PUT
from stockscan.proposals.engine import (
    MIN_ADV_20D,
    MIN_HV_PERCENTILE,
    MIN_PRICE,
    propose_candidates,
)
from stockscan.proposals.portfolio import (
    OPTIONS_RISK_PCT,
    book_multiplier,
    build_book,
    contracts_for,
)
from stockscan.regime import MarketRegime, regime_label


def _leg(strike=100.0, pct_otm=10.0, vol_pct=40.0, confluences=(), price=2.0, delta=0.15):
    return SimpleNamespace(
        strike=strike, pct_otm=pct_otm, vol_pct=vol_pct,
        confluences=confluences, price=price, delta=delta,
    )


def _mk(
    symbol="TST", day_move=-3.0, residual=None, sigma=2.0, trend="up", dte=6,
    days_to_earnings=None, earnings_known=True, hv_percentile=60.0,
    adv_20d=50e6, last_close=100.0, call=None, put=None,
):
    """A fake SymbolAnalysis. ``residual`` defaults to the raw move (sector flat)."""
    sset = SimpleNamespace(
        days_to_expiry=dte, expiry_date=date(2026, 6, 26),
        call=call or _leg(strike=110, pct_otm=10),
        put=put or _leg(strike=90, pct_otm=-10),
    )
    oc = SimpleNamespace(
        available=True, strike_sets=[sset], days_to_earnings=days_to_earnings,
        earnings_known=earnings_known,
    )
    return SimpleNamespace(
        symbol=symbol, available=True, last_close=last_close, adv_20d=adv_20d,
        day_move_pct=day_move,
        day_move_residual_pct=day_move if residual is None else residual,
        daily_sigma_pct=sigma,
        trend=SimpleNamespace(bucket=trend),
        volatility=SimpleNamespace(hv_percentile=hv_percentile),
        options_context=oc,
    )


def _regime(vol_scalar=0.8, stress=False, gate_open=True):
    return MarketRegime(
        as_of_date=date(2026, 6, 15),
        regime=regime_label(trend_gate_open=gate_open, credit_stress_flag=stress),
        trend_gate_open=gate_open,
        days_on_side=12,
        spy_close=Decimal("500"),
        spy_sma200=Decimal("480"),
        spy_sma200_slope_20d=Decimal("0.01"),
        realized_vol_20d=Decimal("0.20"),
        realized_vol_pct_rank=Decimal("0.90"),
        vol_scalar=Decimal(str(vol_scalar)) if vol_scalar is not None else None,
        hy_oas_level=Decimal("3.5"),
        hy_oas_pct_rank=Decimal("0.40"),
        credit_stress_flag=stress,
    )


# ---- trigger ---------------------------------------------------------------
def test_trigger_fires_at_the_same_sigma_for_low_and_high_vol_names():
    # 20%-vol name: daily σ ≈ 1.26%; 80%-vol name: daily σ ≈ 5.04%. A −1.2σ day
    # fires for both, a −0.8σ day for neither — the absolute % is irrelevant.
    low, high = 20 / sqrt(252), 80 / sqrt(252)
    fires = propose_candidates([
        _mk(symbol="LOW", day_move=-1.2 * low, sigma=low),
        _mk(symbol="HIGH", day_move=-1.2 * high, sigma=high),
    ])
    assert [p.symbol for p in fires] == ["LOW", "HIGH"]
    assert all(p.move_sigma == pytest.approx(-1.2, abs=1e-3) for p in fires)
    assert propose_candidates([
        _mk(symbol="LOW", day_move=-0.8 * low, sigma=low),
        _mk(symbol="HIGH", day_move=-0.8 * high, sigma=high),
    ]) == []


def test_missing_daily_sigma_cannot_trigger():
    assert propose_candidates([_mk(day_move=-3.0, sigma=None)]) == []


def test_put_trigger_uses_the_residual_move():
    # Down 3% raw on a day the sector fell as much: beta, not a dip -> no put.
    assert propose_candidates([_mk(day_move=-3.0, residual=-0.5)]) == []
    # Down 1% raw but 3% net of the sector: a relative dip -> put on the residual.
    [p] = propose_candidates([_mk(day_move=-1.0, residual=-3.0)])
    assert p.side == SELL_PUT
    assert p.move_sigma == pytest.approx(-1.5)
    assert p.day_move_pct == -1.0 and p.day_move_residual_pct == -3.0


def test_put_trigger_needs_a_residual():
    # No sector composite -> no residual -> a red day cannot fire a put-sale.
    a = _mk(day_move=-3.0)
    a.day_move_residual_pct = None
    assert propose_candidates([a]) == []


def test_call_trigger_uses_the_raw_move():
    # Up 3% raw, flat vs. the sector: still a green day into the strike.
    [p] = propose_candidates([_mk(day_move=3.0, residual=0.0, trend="down")])
    assert p.side == SELL_CALL
    assert p.move_sigma == pytest.approx(1.5)


# ---- side × trend × regime -------------------------------------------------
def test_red_day_uptrend_sells_put_with_trend():
    [p] = propose_candidates([_mk(day_move=-3.0, trend="up")])
    assert p.side == SELL_PUT and p.trend_align == 1.0


def test_green_day_downtrend_sells_call_with_trend():
    [p] = propose_candidates([_mk(day_move=3.0, trend="down")])
    assert p.side == SELL_CALL and p.trend_align == 1.0


def test_green_day_uptrend_call_is_counter_trend():
    [p] = propose_candidates([_mk(day_move=3.0, trend="up")])
    assert p.side == SELL_CALL and p.trend_align < 0.5


def test_green_day_breakout_is_skipped():
    assert propose_candidates([_mk(day_move=3.0, trend="strong_up")]) == []


def test_strong_down_red_day_is_a_falling_knife():
    assert propose_candidates([_mk(day_move=-3.0, trend="strong_down")]) == []
    [p] = propose_candidates([_mk(day_move=-3.0, trend="down")])
    assert p.trend_align == 0.35


def test_closed_gate_demotes_put_sales_keeps_call_sales():
    closed = _regime(gate_open=False)
    [put] = propose_candidates([_mk(day_move=-3.0, trend="up")], closed)
    assert put.side == SELL_PUT and put.trend_align == 0.35
    [call] = propose_candidates([_mk(day_move=3.0, trend="down")], closed)
    assert call.side == SELL_CALL and call.trend_align == 1.0


def test_credit_stress_skips_put_sales():
    stress = _regime(stress=True)
    assert propose_candidates([_mk(day_move=-3.0, trend="up")], stress) == []
    [call] = propose_candidates([_mk(day_move=3.0, trend="down")], stress)
    assert call.side == SELL_CALL


# ---- hard filters ----------------------------------------------------------
def test_earnings_inside_expiry_dropped_only_when_known():
    assert propose_candidates([_mk(dte=6, days_to_earnings=5)]) == []
    [p] = propose_candidates([_mk(dte=6, days_to_earnings=20)])
    assert p.days_to_earnings == 20


def test_unknown_earnings_is_a_flag_not_a_pass():
    [p] = propose_candidates([_mk(days_to_earnings=None, earnings_known=False)])
    assert p.earnings_known is False
    assert "earnings: unknown" in p.rationale
    [q] = propose_candidates([_mk(days_to_earnings=None, earnings_known=True)])
    assert q.earnings_known is True and "unknown" not in q.rationale


def test_hv_percentile_floor():
    assert propose_candidates([_mk(hv_percentile=MIN_HV_PERCENTILE - 1)]) == []
    assert len(propose_candidates([_mk(hv_percentile=MIN_HV_PERCENTILE)])) == 1
    assert len(propose_candidates([_mk(hv_percentile=None)])) == 1  # unranked passes


def test_adv_20d_floor():
    assert propose_candidates([_mk(adv_20d=MIN_ADV_20D - 1)]) == []
    assert len(propose_candidates([_mk(adv_20d=MIN_ADV_20D)])) == 1


def test_price_floor():
    assert propose_candidates([_mk(last_close=MIN_PRICE - 0.01)]) == []
    assert len(propose_candidates([_mk(last_close=MIN_PRICE)])) == 1


# ---- rank ------------------------------------------------------------------
def test_bigger_residual_dip_with_trend_ranks_first():
    cands = propose_candidates([
        _mk(symbol="SMALL", day_move=-2.5, trend="up"),          # −1.25σ × 1.0
        _mk(symbol="BIG", day_move=-4.0, trend="up"),            # −2.0σ × 1.0
        _mk(symbol="COUNTER", day_move=-6.0, trend="down"),      # −3.0σ × 0.35 = 1.05
    ])
    assert [p.symbol for p in cands] == ["BIG", "SMALL", "COUNTER"]
    assert cands[0].rank_key == pytest.approx(2.0)
    assert cands[2].rank_key == pytest.approx(1.05)


def test_hv_percentile_breaks_rank_ties():
    cands = propose_candidates([
        _mk(symbol="LO", hv_percentile=40.0),
        _mk(symbol="HI", hv_percentile=90.0),
    ])
    assert [p.symbol for p in cands] == ["HI", "LO"]


def test_breakdown_is_the_rank_inputs_only():
    [p] = propose_candidates([_mk()])
    assert set(p.score_breakdown) == {"move_sigma", "trend_align", "hv_percentile", "rank_key"}
    assert "confluence" not in p.score_breakdown
    assert p.score_breakdown["rank_key"] == p.rank_key


def test_confluences_are_carried_as_a_fact_not_ranked():
    plain = _mk(symbol="A")
    on_ema = _mk(symbol="B", put=_leg(strike=90, pct_otm=-10, confluences=("50 EMA $90.10",)))
    by_symbol = {p.symbol: p for p in propose_candidates([plain, on_ema])}
    assert by_symbol["A"].rank_key == by_symbol["B"].rank_key
    assert by_symbol["A"].confluences == ()
    assert by_symbol["B"].confluences == ("50 EMA $90.10",)


# ---- what a seller reads ---------------------------------------------------
def test_sigma_distance_and_credit_yield_arithmetic():
    [p] = propose_candidates([_mk(dte=7, put=_leg(strike=90, pct_otm=-10, vol_pct=40.0, price=0.9))])
    assert p.sigma_distance == pytest.approx(10 / (40 * sqrt(7 / 252)), abs=0.01)
    assert p.credit_yield_ann == pytest.approx(0.9 / 90 * 365 / 7 * 100, abs=0.01)
    assert "1.5σ" in p.rationale and "HV~40%" in p.rationale


# ---- portfolio: multiplier, contracts, diversification ---------------------
def test_book_multiplier():
    assert book_multiplier(None) == 1.0
    assert book_multiplier(_regime(0.8)) == pytest.approx(0.8)
    assert book_multiplier(_regime(0.8, stress=True)) == pytest.approx(0.4)
    assert book_multiplier(_regime(vol_scalar=None)) == 1.0


def test_contracts_against_fixed_equity():
    [p] = propose_candidates([_mk(dte=7, put=_leg(strike=90, pct_otm=-10, vol_pct=40.0))])
    sigma_tenor = 0.40 * sqrt(7 / 252)
    expected = floor(100_000 * OPTIONS_RISK_PCT * 0.8 / (90 * 100 * 2 * sigma_tenor))
    assert contracts_for(p, equity=100_000, book_mult=0.8) == expected
    [row] = build_book([p], _regime(0.8), equity=100_000)
    assert row.contracts == expected and row.size_weight == 0.8
    # Halved under stress, so the row sizes down with the book.
    [stressed] = build_book([p], _regime(0.8, stress=True), equity=100_000)
    assert stressed.contracts == floor(100_000 * OPTIONS_RISK_PCT * 0.4 / (90 * 100 * 2 * sigma_tenor))


def test_sector_cap_limits_names_per_sector():
    cands = propose_candidates([_mk(symbol=s) for s in ("A", "B", "C", "D")])
    sectors = {"A": "Tech", "B": "Tech", "C": "Tech", "D": "Energy"}
    book = build_book(cands, _regime(), equity=100_000, sectors=sectors)
    assert [p.symbol for p in book] == ["A", "B", "D"]


def test_cluster_cap_limits_correlated_names():
    cands = propose_candidates([_mk(symbol=s) for s in ("CORZ", "APLD", "CRWV")])
    assert len(cands) == 3
    assert len(build_book(cands, _regime(), equity=100_000)) == 2


def test_one_per_symbol_and_n_limit():
    cands = propose_candidates([_mk(symbol=f"S{i}") for i in range(10)])
    book = build_book(cands, _regime(), equity=100_000, n=4)
    assert len(book) == 4
    assert len({p.symbol for p in book}) == 4


# ---- macro line ------------------------------------------------------------
def test_macro_events_inside_expiry_are_formatted_for_the_header(monkeypatch):
    seen = {}

    def _fake(*, start, end, importance_min, session=None):
        seen.update(start=start, end=end, importance_min=importance_min)
        return [
            SimpleNamespace(event_type="CPI", event_ts=datetime(2026, 6, 18, 12, 30, tzinfo=timezone.utc)),
            SimpleNamespace(event_type="FOMC", event_ts=datetime(2026, 6, 24, 18, 0, tzinfo=timezone.utc)),
        ]

    monkeypatch.setattr(service, "upcoming_events", _fake)
    assert service.macro_events_inside(date(2026, 6, 15), 9) == ["CPI Thu", "FOMC Wed"]
    assert seen["importance_min"] == "high"
    assert seen["start"].date() == date(2026, 6, 15) and seen["end"].date() == date(2026, 6, 24)


def test_macro_events_soft_fail_to_empty(monkeypatch):
    def _boom(**kwargs):
        raise RuntimeError("no table")

    monkeypatch.setattr(service, "upcoming_events", _boom)
    assert service.macro_events_inside(date(2026, 6, 15), 9) == []


# ---- end-to-end pipeline (needs the DB-backed analysis/regime) ------------
@pytest.mark.integration
def test_generate_book_end_to_end():
    from stockscan.proposals import generate_book

    run = generate_book(n=10)
    assert isinstance(run.candidates, int)
    assert len(run.book) <= 10
    assert all(p.rank_key > 0 and p.size_weight >= 0 for p in run.book)
