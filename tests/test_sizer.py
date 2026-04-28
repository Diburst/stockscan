"""Position sizer (DESIGN §4.7) — risk math is load-bearing; test it carefully."""

from decimal import Decimal

from stockscan.risk.sizer import position_size


def test_basic_1pct_risk() -> None:
    # $1M equity, 1% risk = $10k. Entry $100, stop $90 → $10/share risk → 1000 shares.
    r = position_size(
        equity=Decimal("1000000"),
        entry_price=Decimal("100"),
        stop_price=Decimal("90"),
        risk_pct=Decimal("0.01"),
    )
    assert r.qty == 1000
    assert r.risk_dollars == Decimal("10000.00")
    assert r.notional == Decimal("100000.00")
    assert r.rejected_reason is None


def test_floor_to_integer_shares() -> None:
    # $10k risk / $7/share = 1428.57 → 1428 shares
    r = position_size(
        equity=Decimal("1000000"),
        entry_price=Decimal("100"),
        stop_price=Decimal("93"),
        risk_pct=Decimal("0.01"),
    )
    assert r.qty == 1428


def test_max_position_pct_caps_qty() -> None:
    # Without cap: 1000 shares @ $100 = $100k = 10% of $1M. Cap at 8% → 800 shares.
    r = position_size(
        equity=Decimal("1000000"),
        entry_price=Decimal("100"),
        stop_price=Decimal("90"),
        risk_pct=Decimal("0.01"),
        max_position_pct=Decimal("0.08"),
    )
    assert r.qty == 800
    assert r.notional == Decimal("80000.00")


def test_stop_above_entry_rejected() -> None:
    r = position_size(
        equity=Decimal("1000000"),
        entry_price=Decimal("100"),
        stop_price=Decimal("110"),
        risk_pct=Decimal("0.01"),
    )
    assert r.qty == 0
    assert r.rejected_reason == "stop_above_entry"


def test_zero_equity_rejected() -> None:
    r = position_size(
        equity=Decimal("0"),
        entry_price=Decimal("100"),
        stop_price=Decimal("90"),
        risk_pct=Decimal("0.01"),
    )
    assert r.qty == 0
    assert r.rejected_reason == "no_equity"


def test_qty_zero_when_risk_too_small_for_one_share() -> None:
    # $1 risk, $10/share risk → 0.1 share → 0
    r = position_size(
        equity=Decimal("100"),
        entry_price=Decimal("100"),
        stop_price=Decimal("90"),
        risk_pct=Decimal("0.01"),
    )
    assert r.qty == 0
    assert r.rejected_reason == "qty_zero"
