"""Tests for the sector_rs technical indicator.

The pure math (`_rs_values`) is exercised directly (no DB). The DB-backed
`values()` path (composite fetch) is integration-tested in the user's
environment; here we cover the relative-strength permutations, the slope, the
abstain cases, the tag-aware scoring, and that the indicator auto-registers.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from stockscan.technical.indicators.sector_rs import (
    SectorRSParams,
    TechnicalSectorRS,
    _rs_values,
)

LOOK = 10
SLOPE_W = 4
BAND = 0.15
SLOPE_BAND = 0.05
N = 40


def _ramp(p_then: float, p_now: float, n: int = N, look: int = LOOK) -> pd.Series:
    """Series flat at p_then, then linear from p_then to p_now over the last
    `look`+1 bars — so iloc[-1]/iloc[-1-look] == p_now/p_then exactly."""
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    arr = np.empty(n, dtype=float)
    start = n - 1 - look
    arr[:start] = p_then
    arr[start:] = np.linspace(p_then, p_now, look + 1)
    return pd.Series(arr, index=idx)


def _rs(stock: pd.Series, sector: pd.Series):
    return _rs_values(
        stock, sector, look=LOOK, band=BAND, slope_window=SLOPE_W, slope_band=SLOPE_BAND
    )


class TestRSPermutations:
    def test_leader_in_rising_sector(self):
        v = _rs(_ramp(100, 120), _ramp(100, 105))  # +20% vs +5%
        assert v["stock_ret"] == pytest.approx(0.20)
        assert v["sector_ret"] == pytest.approx(0.05)
        assert v["spread"] == pytest.approx(0.15)
        assert v["rs"] == pytest.approx(1.0)  # 0.15 / 0.15 saturates

    def test_laggard_in_rising_sector(self):
        v = _rs(_ramp(100, 105), _ramp(100, 120))  # +5% vs +20%
        assert v["spread"] == pytest.approx(-0.15)
        assert v["rs"] == pytest.approx(-1.0)

    def test_resilient_in_falling_sector_is_positive(self):
        # "down less than sector" → relative strength → positive (a bottom tilt)
        v = _rs(_ramp(100, 95), _ramp(100, 80))  # -5% vs -20%
        assert v["spread"] == pytest.approx(0.15)
        assert v["rs"] == pytest.approx(1.0)

    def test_partial_spread_scales_linearly(self):
        v = _rs(_ramp(100, 107.5), _ramp(100, 100))  # +7.5% vs 0%
        assert v["spread"] == pytest.approx(0.075)
        assert v["rs"] == pytest.approx(0.5)

    def test_leader_has_nonnegative_slope(self):
        v = _rs(_ramp(100, 120), _ramp(100, 105))
        assert v["slope_n"] >= 0.0  # RS line rising as the stock outperforms


class TestAbstain:
    def test_too_short_returns_none(self):
        short = pd.Series(np.arange(LOOK), dtype=float)  # len == LOOK, need > LOOK
        assert _rs(short, short) is None

    def test_all_nan_sector_returns_none(self):
        stock = _ramp(100, 120)
        sec = pd.Series(np.nan, index=stock.index)
        assert _rs(stock, sec) is None

    def test_zero_base_returns_none(self):
        assert _rs(_ramp(0.0, 120), _ramp(100, 105)) is None


class TestScore:
    class _MR:
        tags = ("mean_reversion", "long_only", "swing")

    class _Trend:
        tags = ("trend_following", "breakout")

    def _ind(self):
        return TechnicalSectorRS(SectorRSParams())

    def test_neutral_mode_full_signed(self):
        ind = self._ind()
        assert ind.score({"rs": 1.0, "slope_n": 0.0}, None) == pytest.approx(0.7)

    def test_mean_reversion_dampened(self):
        ind = self._ind()
        # raw = 0.7*1 + 0.3*0 = 0.7; MR dampen 0.6 → 0.42
        assert ind.score({"rs": 1.0, "slope_n": 0.0}, self._MR) == pytest.approx(0.42)
        # top side keeps the sign
        assert ind.score({"rs": -1.0, "slope_n": 0.0}, self._MR) == pytest.approx(-0.42)

    def test_trend_full_strength(self):
        ind = self._ind()
        assert ind.score({"rs": 1.0, "slope_n": 1.0}, self._Trend) == pytest.approx(1.0)


class TestRegistration:
    def test_indicator_is_registered(self):
        from stockscan.technical.indicators import (
            TECH_REGISTRY,
            discover_technical_indicators,
        )

        discover_technical_indicators()
        assert "sector_rs" in TECH_REGISTRY.names()
