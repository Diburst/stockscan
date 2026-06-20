"""Unit tests for the options-proposal engine (pure; mock SymbolAnalysis)."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from stockscan.proposals._models import SELL_CALL, SELL_PUT
from stockscan.proposals.engine import propose_candidates
from stockscan.proposals.portfolio import build_book, regime_size_multiplier


def _leg(strike=100.0, pct_otm=10.0, vol_pct=90.0, confluences=(), price=2.0, delta=0.15):
    return SimpleNamespace(
        strike=strike, pct_otm=pct_otm, vol_pct=vol_pct,
        confluences=confluences, price=price, delta=delta,
    )


def _mk(
    symbol="TST", day_move=-3.0, trend="up", dte=6, days_to_earnings=None,
    pct_to_support=8.0, pct_to_resistance=8.0, last_volume=50e6,
    call=None, put=None,
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
        pct_to_support=pct_to_support, pct_to_resistance=pct_to_resistance,
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


def test_green_day_at_resistance_downtrend_sells_call():
    [p] = propose_candidates([_mk(day_move=3.0, trend="down", pct_to_resistance=2.0)])
    assert p.side == SELL_CALL


def test_green_day_breakout_is_skipped():
    # strong_up momentum into resistance -> do NOT sell a call into a breakout
    assert propose_candidates([_mk(day_move=3.0, trend="strong_up", pct_to_resistance=2.0)]) == []


def test_green_day_open_space_is_skipped():
    # green but not near resistance -> no qualifying call sale
    assert propose_candidates([_mk(day_move=3.0, trend="down", pct_to_resistance=12.0)]) == []


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


def test_price_at_level_flag_is_context_only():
    # Price sitting ON support (red day) -> flagged; far from support -> not.
    [at] = propose_candidates([_mk(day_move=-3.0, trend="up", pct_to_support=1.5)])
    [far] = propose_candidates([_mk(day_move=-3.0, trend="up", pct_to_support=8.0)])
    assert at.price_at_level is True
    assert far.price_at_level is False
    # It must NOT change the score (context flag only) — same inputs otherwise,
    # so the score is driven by 'room' (pct_to_threat), which differs here; the
    # flag itself isn't a score input.
    assert "price_at_level" not in at.score_breakdown


def test_score_is_bounded_and_has_breakdown():
    [p] = propose_candidates([_mk(day_move=-3.0, trend="up")])
    assert 0.0 <= p.score <= 1.0
    assert {"premium", "room", "confluence", "trend_align", "daycolor", "score"} <= set(
        p.score_breakdown
    )


# ---- portfolio sizing + diversification -----------------------------------
def _regime(composite=0.68, stress=False, breadth=0.27):
    return SimpleNamespace(
        composite_score=composite, credit_stress_flag=stress, breadth_score=breadth
    )


def test_regime_size_multiplier():
    assert regime_size_multiplier(None) == 1.0
    assert regime_size_multiplier(_regime(0.68)) == pytest.approx(0.84)  # 0.5 + 0.5*0.68
    assert regime_size_multiplier(_regime(0.68, stress=True)) == pytest.approx(0.42)  # ×0.5


def test_short_call_haircut_in_weak_breadth():
    call_cand = propose_candidates([_mk(day_move=3.0, trend="down", pct_to_resistance=2.0)])
    book = build_book(call_cand, _regime(breadth=0.27))
    # 0.84 regime × 0.70 short-call breadth haircut
    assert book[0].size_weight == round(0.84 * 0.70, 3)


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
