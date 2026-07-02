"""Pure P&L + settlement math for the delta hedge."""

from __future__ import annotations

import pytest

from stockscan.hedge import accounting


# ---- signed average-cost fills ----
def test_apply_fill_adds_and_averages():
    f = accounting.apply_fill(held_shares=0, avg_cost=0.0, realized_pnl=0.0, fill_qty=50, fill_price=100.0)
    assert (f.held_shares, f.avg_cost, f.realized_pnl) == (50, 100.0, 0.0)
    f = accounting.apply_fill(held_shares=50, avg_cost=100.0, realized_pnl=0.0, fill_qty=50, fill_price=110.0)
    assert f.held_shares == 100
    assert f.avg_cost == pytest.approx(105.0)
    assert f.realized_pnl == 0.0


def test_apply_fill_partial_close_realises_pnl():
    f = accounting.apply_fill(held_shares=100, avg_cost=105.0, realized_pnl=0.0, fill_qty=-40, fill_price=120.0)
    assert f.held_shares == 60
    assert f.avg_cost == pytest.approx(105.0)  # unchanged on the remainder
    assert f.realized_delta == pytest.approx((120.0 - 105.0) * 40)
    assert f.realized_pnl == pytest.approx(600.0)


def test_apply_fill_flip_through_zero():
    f = accounting.apply_fill(held_shares=60, avg_cost=105.0, realized_pnl=0.0, fill_qty=-100, fill_price=120.0)
    assert f.held_shares == -40
    assert f.avg_cost == pytest.approx(120.0)  # residual opens fresh at fill price
    assert f.realized_pnl == pytest.approx((120.0 - 105.0) * 60)


def test_apply_fill_short_side_realises_correctly():
    # Short 50 @ 100, cover 50 @ 90 → +500 on a short.
    f = accounting.apply_fill(held_shares=-50, avg_cost=100.0, realized_pnl=0.0, fill_qty=50, fill_price=90.0)
    assert f.held_shares == 0
    assert f.realized_pnl == pytest.approx((100.0 - 90.0) * 50)


# ---- option marks ----
def test_option_mark_short_vs_long_pnl_opposite_sign():
    kw = dict(spot=1300, strike=1300, t=30 / 365, r=0.04, sigma=0.30, kind="call", contracts=1, premium=2500.0)
    v_short, pnl_short = accounting.option_mark(option_side="short", **kw)
    v_long, pnl_long = accounting.option_mark(option_side="long", **kw)
    assert v_short == pytest.approx(v_long)
    assert pnl_short == pytest.approx(-pnl_long)


def test_hedge_unrealized_signed():
    assert accounting.hedge_unrealized(100, 1300.0, 1310.0) == pytest.approx(1000.0)
    assert accounting.hedge_unrealized(-100, 1300.0, 1310.0) == pytest.approx(-1000.0)


# ---- settlement ----
def test_settle_deep_itm_short_call_perfectly_hedged_nets_premium():
    # Short 1x 1300 call, premium 2500, holding the full 100-share hedge at 1300.
    s = accounting.settle(
        kind="call", option_side="short", strike=1300, contracts=1, premium=2500.0,
        held_shares=100, avg_cost=1300.0, realized_hedge_pnl=0.0, spot=1400.0,
    )
    assert s.in_the_money
    assert s.option_settlement_pnl == pytest.approx(2500.0 - 10000.0)  # pay 100pt intrinsic
    assert s.realized_hedge_pnl == pytest.approx(10000.0)  # sell hedge 1300→1400
    assert s.total_realized_pnl == pytest.approx(2500.0)  # nets the premium


def test_settle_otm_short_call_keeps_premium():
    s = accounting.settle(
        kind="call", option_side="short", strike=1300, contracts=1, premium=2500.0,
        held_shares=0, avg_cost=0.0, realized_hedge_pnl=0.0, spot=1250.0,
    )
    assert not s.in_the_money
    assert s.total_realized_pnl == pytest.approx(2500.0)
    assert s.unwind_fill_qty == 0


def test_settle_long_put_itm_collects_intrinsic():
    s = accounting.settle(
        kind="put", option_side="long", strike=1300, contracts=1, premium=2000.0,
        held_shares=0, avg_cost=0.0, realized_hedge_pnl=0.0, spot=1200.0,
    )
    assert s.in_the_money
    assert s.option_settlement_pnl == pytest.approx(10000.0 - 2000.0)


def test_net_open_pnl_sums_three_legs():
    assert accounting.net_open_pnl(
        option_open_pnl=100.0, realized_hedge_pnl=50.0, hedge_unrealized_pnl=-30.0
    ) == pytest.approx(120.0)
