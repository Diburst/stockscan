"""Unit + property tests for the pure sector-composite math.

Imports only ``stockscan.sectors.composite`` (no DB/config), so this runs
without infrastructure. The crown-jewel invariant — mirroring
``tests/test_regime_composite.py`` — is **no look-ahead**: a sector level at
date ``t`` recomputed on the truncated prefix ``≤ t`` must equal the live value
at ``t``.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from stockscan.sectors.composite import (
    COMPOSITE_PREFIX,
    build_sector_composites,
    chain_to_levels,
    composite_symbol,
    daily_returns,
    sector_code,
    sector_daily_returns,
)


def _dates(n: int):
    return pd.date_range("2024-01-01", periods=n, freq="B")


# ======================================================================
# sector_code / composite_symbol
# ======================================================================
class TestNaming:
    @pytest.mark.parametrize(
        "name,code",
        [
            ("Technology", "TECHNOLOGY"),
            ("Financial Services", "FINANCIAL_SERVICES"),
            ("  Consumer Cyclical  ", "CONSUMER_CYCLICAL"),
            ("Real-Estate", "REAL_ESTATE"),
            ("Health/Care", "HEALTH_CARE"),
        ],
    )
    def test_sector_code_slugify(self, name, code):
        assert sector_code(name) == code

    def test_composite_symbol(self):
        assert composite_symbol("Financial Services") == f"{COMPOSITE_PREFIX}FINANCIAL_SERVICES"
        assert composite_symbol("TECHNOLOGY", is_code=True) == f"{COMPOSITE_PREFIX}TECHNOLOGY"


# ======================================================================
# Hand-checked equal-weight chaining
# ======================================================================
class TestHandChecked:
    def test_two_symbol_equal_weight_index(self):
        idx = _dates(4)
        closes = pd.DataFrame(
            {
                "A": [100.0, 110.0, 99.0, 99.0],
                "B": [50.0, 55.0, 60.0, 54.0],
            },
            index=idx,
        )
        sector_of = {"A": "Technology", "B": "Technology"}
        out = build_sector_composites(closes, sector_of, base=100.0)

        level = out["TECHNOLOGY"]
        # d0 base; d1 +10% both → 110; d2 mean(-10%, +9.0909%) = -0.45455% → 109.5;
        # d3 mean(0%, -10%) = -5% → 104.025
        expected = [100.0, 110.0, 109.5, 104.025]
        assert level.tolist() == pytest.approx(expected, rel=1e-9)

    def test_missing_bar_excludes_that_symbol_that_day(self):
        idx = _dates(4)
        closes = pd.DataFrame(
            {
                "A": [100.0, 110.0, 99.0, 99.0],
                "B": [50.0, 55.0, 60.0, np.nan],  # no bar on d3
            },
            index=idx,
        )
        out = build_sector_composites(closes, {"A": "Technology", "B": "Technology"})
        level = out["TECHNOLOGY"]
        # d3 uses A only (0%): 109.5 * 1.0 = 109.5
        assert level.iloc[-1] == pytest.approx(109.5, rel=1e-9)

    def test_membership_mask_excludes_nonmembers(self):
        idx = _dates(4)
        closes = pd.DataFrame(
            {"A": [100.0, 110.0, 99.0, 99.0], "B": [50.0, 55.0, 60.0, 54.0]},
            index=idx,
        )
        membership = pd.DataFrame(True, index=idx, columns=["A", "B"])
        membership.loc[idx[3], "B"] = False  # B not a member on d3
        out = build_sector_composites(
            closes, {"A": "Technology", "B": "Technology"}, membership=membership
        )
        # d3 uses A only (0%): 109.5 stays
        assert out["TECHNOLOGY"].iloc[-1] == pytest.approx(109.5, rel=1e-9)

    def test_min_members_holds_level_flat(self):
        idx = _dates(3)
        closes = pd.DataFrame(
            {"A": [100.0, 110.0, 120.0], "B": [50.0, np.nan, np.nan]},
            index=idx,
        )
        # Require 2 members; only A has data on d1/d2 → those days NaN → flat.
        out = build_sector_composites(
            closes, {"A": "Technology", "B": "Technology"}, min_members=2
        )
        assert out["TECHNOLOGY"].tolist() == pytest.approx([100.0, 100.0, 100.0])

    def test_two_sectors_separated(self):
        idx = _dates(3)
        closes = pd.DataFrame(
            {
                "A": [100.0, 110.0, 121.0],  # Tech, +10%/day
                "T2": [10.0, 11.0, 12.1],    # Tech, +10%/day
                "F": [200.0, 190.0, 180.5],  # Fin, -5%/day
            },
            index=idx,
        )
        sector_of = {"A": "Technology", "T2": "Technology", "F": "Financial Services"}
        out = build_sector_composites(closes, sector_of)
        assert set(out) == {"TECHNOLOGY", "FINANCIAL_SERVICES"}
        assert out["TECHNOLOGY"].tolist() == pytest.approx([100.0, 110.0, 121.0])
        assert out["FINANCIAL_SERVICES"].tolist() == pytest.approx([100.0, 95.0, 90.25])

    def test_symbols_without_sector_are_ignored(self):
        idx = _dates(2)
        closes = pd.DataFrame({"A": [100.0, 110.0], "X": [5.0, 6.0]}, index=idx)
        out = build_sector_composites(closes, {"A": "Technology"})  # X unmapped
        assert set(out) == {"TECHNOLOGY"}


# ======================================================================
# No-look-ahead — the crown-jewel invariant
# ======================================================================
class TestNoLookAhead:
    @pytest.fixture
    def panel(self):
        rng = np.random.default_rng(11)
        n, k = 300, 12
        idx = _dates(n)
        syms = [f"S{i}" for i in range(k)]
        # geometric random walks, with scattered missing bars
        prices = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.015, size=(n, k)), axis=0))
        closes = pd.DataFrame(prices, index=idx, columns=syms)
        miss = rng.random((n, k)) < 0.03
        closes = closes.mask(miss)
        sectors = ["Technology", "Energy", "Healthcare"]
        sector_of = {s: sectors[i % len(sectors)] for i, s in enumerate(syms)}
        # membership: each symbol joins at a random early date
        membership = pd.DataFrame(True, index=idx, columns=syms)
        for j, s in enumerate(syms):
            join_at = int(rng.integers(0, 30))
            membership.iloc[:join_at, j] = False
        return closes, sector_of, membership

    @pytest.mark.parametrize("truncate_at", [120, 180, 240, 299])
    def test_levels_truncation_invariant(self, panel, truncate_at):
        closes, sector_of, membership = panel
        full = build_sector_composites(closes, sector_of, membership=membership)
        trunc = build_sector_composites(
            closes.iloc[: truncate_at + 1],
            sector_of,
            membership=membership.iloc[: truncate_at + 1],
        )
        for code, lvl in trunc.items():
            assert lvl.iloc[-1] == pytest.approx(full[code].iloc[truncate_at], rel=1e-12)

    @pytest.mark.parametrize("truncate_at", [100, 200, 299])
    def test_sector_daily_returns_truncation_invariant(self, panel, truncate_at):
        closes, sector_of, membership = panel
        full = sector_daily_returns(daily_returns(closes), sector_of, membership=membership)
        trunc = sector_daily_returns(
            daily_returns(closes.iloc[: truncate_at + 1]),
            sector_of,
            membership=membership.iloc[: truncate_at + 1],
        )
        for code in trunc.columns:
            a, b = trunc[code].iloc[-1], full[code].iloc[truncate_at]
            if pd.isna(a) or pd.isna(b):
                assert pd.isna(a) and pd.isna(b)
            else:
                assert a == pytest.approx(b, rel=1e-12)


# ======================================================================
# chain_to_levels edge cases
# ======================================================================
class TestChain:
    def test_empty(self):
        assert chain_to_levels(pd.Series([], dtype=float)).empty

    def test_leading_nan_starts_at_base(self):
        s = pd.Series([np.nan, 0.10, -0.05])
        lvl = chain_to_levels(s, base=100.0)
        assert lvl.tolist() == pytest.approx([100.0, 110.0, 104.5])
