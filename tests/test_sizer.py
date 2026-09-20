"""Position sizer (DESIGN §4.7) — risk math is load-bearing; test it carefully.

``position_size`` is the pure rule; ``size_for_strategy`` reads the sizing
attributes off a strategy class and applies the regime vol scalar. The
fakes below are plain classes: only the class attributes are read.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from stockscan.risk.sizer import SizingResult, position_size, size_for_strategy

EQUITY = Decimal("1000000")
CAP = Decimal("0.08")


def _stop_based(**kw) -> SizingResult:
    args = dict(
        equity=EQUITY,
        entry_price=Decimal("100"),
        stop_price=Decimal("90"),
        risk_pct=Decimal("0.01"),
        position_pct=None,
        max_position_pct=Decimal("1"),
    )
    args.update(kw)
    return position_size(**args)


# -----------------------------------------------------------------------
# Stop-based sizing
# -----------------------------------------------------------------------


class TestStopBased:
    def test_basic_1pct_risk(self):
        # $1M × 1% = $10k risk; $10/share risk → 1000 shares.
        r = _stop_based()
        assert r.qty == 1000
        assert r.risk_dollars == Decimal("10000.00")
        assert r.notional == Decimal("100000.00")
        assert r.rejected_reason is None

    def test_floor_to_integer_shares(self):
        # $10k / $7 = 1428.57 → 1428.
        r = _stop_based(stop_price=Decimal("93"))
        assert r.qty == 1428

    def test_stop_wins_over_position_pct_when_both_present(self):
        r = _stop_based(position_pct=Decimal("0.5"))
        assert r.qty == 1000  # stop rule, not 5000 from the fraction

    def test_max_position_pct_caps_qty(self):
        # 1000 shares @ $100 = 10% of equity; cap 8% → 800 shares.
        r = _stop_based(max_position_pct=CAP)
        assert r.qty == 800
        assert r.notional == Decimal("80000.00")
        assert r.rejected_reason is None

    def test_stop_above_entry_rejected(self):
        r = _stop_based(stop_price=Decimal("110"))
        assert r.qty == 0
        assert r.rejected_reason == "stop_above_entry"

    def test_stop_equal_to_entry_rejected(self):
        r = _stop_based(stop_price=Decimal("100"))
        assert r.rejected_reason == "stop_above_entry"

    def test_invalid_risk_pct_rejected(self):
        r = _stop_based(risk_pct=Decimal("0"))
        assert r.qty == 0
        assert r.rejected_reason == "invalid_risk_pct"

    def test_qty_zero_when_risk_too_small_for_one_share(self):
        # $1 risk vs $10/share → 0.1 share → 0.
        r = _stop_based(equity=Decimal("100"))
        assert r.qty == 0
        assert r.rejected_reason == "qty_zero"
        assert r.notional == Decimal(0)


# -----------------------------------------------------------------------
# Fixed-fraction sizing (no stop)
# -----------------------------------------------------------------------


class TestFixedFraction:
    def test_position_pct_sets_qty(self):
        # 10% of $1M = $100k at $100 → 1000 shares; the notional IS the risk.
        r = position_size(
            EQUITY,
            Decimal("100"),
            stop_price=None,
            risk_pct=Decimal("0.01"),
            position_pct=Decimal("0.10"),
            max_position_pct=Decimal("1"),
        )
        assert r.qty == 1000
        assert r.risk_dollars == Decimal("100000.00")
        assert r.notional == Decimal("100000.00")
        assert r.rejected_reason is None

    def test_floors_to_integer(self):
        r = position_size(
            Decimal("1000"),
            Decimal("33"),
            stop_price=None,
            risk_pct=Decimal("0.01"),
            position_pct=Decimal("0.10"),
            max_position_pct=Decimal("1"),
        )
        assert r.qty == 3  # $100 / $33 = 3.03

    def test_max_position_pct_caps_fraction(self):
        r = position_size(
            EQUITY,
            Decimal("100"),
            stop_price=None,
            risk_pct=Decimal("0.01"),
            position_pct=Decimal("0.10"),
            max_position_pct=CAP,
        )
        assert r.qty == 800

    @pytest.mark.parametrize("position_pct", [None, Decimal("0"), Decimal("-0.1")])
    def test_no_stop_and_no_position_pct_rejected(self, position_pct):
        r = position_size(
            EQUITY,
            Decimal("100"),
            stop_price=None,
            risk_pct=Decimal("0.01"),
            position_pct=position_pct,
            max_position_pct=CAP,
        )
        assert r.qty == 0
        assert r.rejected_reason == "no_stop_and_no_position_pct"


# -----------------------------------------------------------------------
# Rejections common to both rules
# -----------------------------------------------------------------------


class TestCommonRejections:
    def test_no_equity(self):
        r = _stop_based(equity=Decimal("0"))
        assert r.qty == 0
        assert r.rejected_reason == "no_equity"

    def test_negative_equity(self):
        r = _stop_based(equity=Decimal("-5"))
        assert r.rejected_reason == "no_equity"

    def test_invalid_entry_price(self):
        r = _stop_based(entry_price=Decimal("0"))
        assert r.qty == 0
        assert r.rejected_reason == "invalid_entry_price"


# -----------------------------------------------------------------------
# size_for_strategy — strategy-declared rule × vol scalar
# -----------------------------------------------------------------------


class _Momentumish:
    default_risk_pct = 0.01
    position_pct = None
    sizes_down_in_high_vol = True


class _MeanRevish:
    default_risk_pct = 0.01
    position_pct = 0.10
    sizes_down_in_high_vol = False


class _FixedFractionSizesDown:
    default_risk_pct = 0.01
    position_pct = 0.10
    sizes_down_in_high_vol = True


def _size(cls, *, stop, vol_scalar=1.0, equity=EQUITY, entry=Decimal("100"), cap=Decimal("1")):
    return size_for_strategy(
        cls, equity, entry, stop, vol_scalar=vol_scalar, max_position_pct=cap
    )


class TestSizeForStrategy:
    def test_stop_based_strategy_uses_default_risk_pct(self):
        r = _size(_Momentumish, stop=Decimal("90"))
        assert r.qty == 1000
        assert r.risk_dollars == Decimal("10000.00")

    def test_fixed_fraction_strategy_uses_position_pct(self):
        r = _size(_MeanRevish, stop=None)
        assert r.qty == 1000
        assert r.notional == Decimal("100000.00")

    def test_stop_based_strategy_without_stop_is_rejected(self):
        r = _size(_Momentumish, stop=None)
        assert r.qty == 0
        assert r.rejected_reason == "no_stop_and_no_position_pct"

    def test_max_position_pct_forwarded(self):
        r = _size(_Momentumish, stop=Decimal("90"), cap=CAP)
        assert r.qty == 800

    def test_vol_scalar_shrinks_sizes_down_strategy(self):
        r = _size(_Momentumish, stop=Decimal("90"), vol_scalar=0.5)
        assert r.qty == 500
        assert r.risk_dollars == Decimal("5000.00")
        assert r.notional == Decimal("50000.00")
        assert r.rejected_reason is None

    def test_vol_scalar_floors_share_count(self):
        r = _size(_Momentumish, stop=Decimal("90"), vol_scalar=0.6667)
        assert r.qty == 666  # int(1000 × 0.6667) = 666, never rounded up

    def test_vol_scalar_applies_after_the_cap(self):
        r = _size(_Momentumish, stop=Decimal("90"), vol_scalar=0.5, cap=CAP)
        assert r.qty == 400  # 800 capped, then halved

    def test_vol_scalar_ignored_when_strategy_opts_out(self):
        r = _size(_MeanRevish, stop=None, vol_scalar=0.5)
        assert r.qty == 1000
        assert r.risk_dollars == Decimal("100000.00")

    def test_vol_scalar_applies_to_fixed_fraction_when_opted_in(self):
        r = _size(_FixedFractionSizesDown, stop=None, vol_scalar=0.5)
        assert r.qty == 500

    @pytest.mark.parametrize("scalar", [1.0, 1.5, 2.0])
    def test_never_scales_up(self, scalar):
        r = _size(_Momentumish, stop=Decimal("90"), vol_scalar=scalar)
        assert r.qty == 1000
        assert r.risk_dollars == Decimal("10000.00")

    def test_vol_scalar_zero_size(self):
        # 1 share at base, scalar 0.5 → 0 shares.
        r = _size(_Momentumish, stop=Decimal("90"), vol_scalar=0.5, equity=Decimal("1000"))
        assert r.qty == 0
        assert r.rejected_reason == "vol_scalar_zero_size"
        assert r.notional == Decimal(0)

    def test_base_rejection_passes_through_untouched(self):
        r = _size(_Momentumish, stop=Decimal("110"), vol_scalar=0.5)
        assert r.qty == 0
        assert r.rejected_reason == "stop_above_entry"
