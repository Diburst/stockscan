"""Unit tests for the options-proposal engine (pure; mock SymbolAnalysis)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from stockscan.proposals._models import SELL_CALL, SELL_PUT
from stockscan.proposals.engine import propose_candidates
from stockscan.proposals.portfolio import build_book, regime_size_multiplier
from stockscan.regime import MarketRegime, regime_label


def _leg(strike=100.0, pct_otm=10.0, vol_pct=90.0, confluences=(), price=2.0, delta=0.15):
    return SimpleNamespace(
        strike=strike, pct_otm=pct_otm, vol_pct=vol_pct,
        confluences=confluences, price=price, delta=delta,
    )


def _mk(
    symbol="TST", day_move=-3.0, trend="up", dte=6, days_to_earnings=None,
    last_volume=50e6, call=None, put=None,
):
    prev = 100.0
    last = prev * (1 + day_move / 100.0)
    sset = SimpleNamespace(
        days_to_expiry=dte, expiry_date=date(2026, 6, 22),
        call=call or _leg(strike=110, pct_otm=10),
        put=put or _leg(strike=90, pct_otm=-10),
    )
    oc = SimpleNamespace(
        available=True, strike_sets=[sset], days_to_earnings=days_to_earnings,
    )
    return SimpleNamespace(
        symbol=symbol, available=True, last_volume=last_volume,
        closes_history=[(date(2026, 6, 15), prev), (date(2026, 6, 16), last)],
        trend=SimpleNamespace(bucket=trend), options_context=oc,
    )


# ---- side selection -------------------------------------------------------
def test_red_day_uptrend_sells_put():
    [p] = propose_candidates([_mk(day_move=-3.0, trend="up")])
    assert p.side == SELL_PUT
    assert p.score_breakdown["trend_align"] == 1.0  # with-trend dip = best


def test_green_day_downtrend_sells_call():
    [p] = propose_candidates([_mk(day_move=3.0, trend="down")])
    assert p.side == SELL_CALL
    assert p.score_breakdown["trend_align"] == 1.0  # with-trend bounce = best


def test_green_day_uptrend_call_is_penalized():
    [p] = propose_candidates([_mk(day_move=3.0, trend="up")])
    assert p.side == SELL_CALL
    assert p.score_breakdown["trend_align"] < 0.5


def test_green_day_breakout_is_skipped():
    # strong_up momentum -> do NOT sell a call into a breakout
    assert propose_candidates([_mk(day_move=3.0, trend="strong_up")]) == []


def test_small_move_no_trigger():
    assert propose_candidates([_mk(day_move=0.5, trend="up")]) == []


# ---- hard filters ---------------------------------------------------------
def test_earnings_inside_expiry_dropped():
    assert propose_candidates([_mk(day_move=-3.0, dte=6, days_to_earnings=5)]) == []


def test_illiquid_dropped():
    assert propose_candidates([_mk(day_move=-3.0, last_volume=1_000_000.0)]) == []


def test_low_iv_dropped():
    low = _mk(day_move=-3.0, put=_leg(strike=90, pct_otm=-10, vol_pct=10.0))
    assert propose_candidates([low]) == []


def test_ema_confluence_raises_score():
    plain = _mk(day_move=-3.0, trend="up")
    on_ema = _mk(
        day_move=-3.0, trend="up",
        put=_leg(strike=90, pct_otm=-10, confluences=("50 EMA $90.10 (0.1% away)",)),
    )
    [p0] = propose_candidates([plain])
    [p1] = propose_candidates([on_ema])
    assert p1.confluence_count == 1 and p0.confluence_count == 0
    assert p1.score > p0.score


def test_score_is_bounded_and_has_breakdown():
    [p] = propose_candidates([_mk(day_move=-3.0, trend="up")])
    assert 0.0 <= p.score <= 1.0
    assert {"premium", "confluence", "trend_align", "daycolor", "score"} <= set(
        p.score_breakdown
    )


# ---- portfolio sizing + diversification -----------------------------------
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


def test_regime_size_multiplier():
    assert regime_size_multiplier(None) == 1.0
    assert regime_size_multiplier(_regime(0.8)) == pytest.approx(0.8)
    assert regime_size_multiplier(_regime(0.8, stress=True)) == pytest.approx(0.4)  # ×0.5
    assert regime_size_multiplier(_regime(vol_scalar=None)) == 1.0  # neutral when uncomputed


def test_closed_trend_gate_does_not_block_the_options_book():
    # The book is short premium: it sizes down under stress, never blocks.
    call_cand = propose_candidates([_mk(day_move=3.0, trend="down")])
    book = build_book(call_cand, _regime(0.8, stress=True, gate_open=False))
    assert len(book) == 1
    assert book[0].size_weight == round(0.8 * 0.5, 3)


def test_cluster_cap_limits_correlated_names():
    cands = propose_candidates(
        [_mk(symbol=s, day_move=-3.0, trend="up") for s in ("CORZ", "APLD", "CRWV")]
    )
    assert len(cands) == 3
    book = build_book(cands, _regime())
    assert len(book) == 2  # CoreWeave cluster capped at 2


def test_one_per_symbol_and_n_limit():
    cands = propose_candidates([_mk(symbol=f"S{i}", day_move=-3.0, trend="up") for i in range(10)])
    book = build_book(cands, _regime(), n=4)
    assert len(book) == 4
    assert len({p.symbol for p in book}) == 4
    assert all(p.size_weight > 0 for p in book)


# ---- end-to-end pipeline (needs the DB-backed analysis/regime) ------------
@pytest.mark.integration
def test_generate_book_end_to_end():
    from stockscan.proposals import generate_book

    run = generate_book(n=10)
    assert isinstance(run.candidates, int)
    assert len(run.book) <= 10
    assert all(0.0 <= p.score <= 1.0 and p.size_weight >= 0 for p in run.book)
