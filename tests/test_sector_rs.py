"""Tests for the relative-strength primitive (stockscan.indicators.relative_strength).

The pure math (`relative_strength_values`) is exercised directly (no DB). The
DB-backed `sector_relative_strength` path (composite fetch) is integration-tested
in the user's environment; here we cover the relative-strength permutations, the
slope, the abstain cases, the signed `raw`, and that the data-fetch wrapper
abstains gracefully when no symbol/composite is resolvable.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from stockscan.indicators.relative_strength import (
    relative_strength_values,
    sector_relative_strength,
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


def _clip(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _rs(stock: pd.Series, sector: pd.Series):
    return relative_strength_values(
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


class TestRawRead:
    """The signed `raw` is the strategy-agnostic read: clip(0.7*rs + 0.3*slope).

    The old per-strategy tag-branching (full strength for trend/breakout, a 0.6
    dampen for mean-reversion, neutral for the watchlist) has been removed — a
    consumer that wants to down-weight relative strength does so via its own
    composite weight, not inside this primitive.
    """

    def test_raw_matches_weighted_combination(self):
        for v in (_rs(_ramp(100, 120), _ramp(100, 105)), _rs(_ramp(100, 105), _ramp(100, 120))):
            assert v["raw"] == pytest.approx(_clip(0.7 * v["rs"] + 0.3 * v["slope_n"]))

    def test_custom_weights_respected(self):
        v = relative_strength_values(
            _ramp(100, 120), _ramp(100, 105),
            look=LOOK, band=BAND, slope_window=SLOPE_W, slope_band=SLOPE_BAND,
            rs_weight=1.0, slope_weight=0.0,
        )
        assert v["raw"] == pytest.approx(_clip(v["rs"]))


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


class TestIntradayTimestampAlignment:
    """The real-world bug surfaced in backtest runs #20 and #21: stock bars
    from EODHD are stored at NY market close (≈ 20–21:00 UTC); sector
    composite bars are stored at midnight UTC. The two indices share calendar
    dates but never share intraday timestamps, so the indicator's reindex +
    ffill produced an all-NaN sec_on and silently abstained on every call.
    These tests pin the fix: alignment must be by calendar date regardless of
    intraday timestamp.
    """

    def _stock_at_ny_close(self, levels: list[float], start: str = "2024-01-01"):
        """Stock series with EODHD-style timestamps — NY market close in UTC."""
        idx = pd.date_range(start, periods=len(levels), freq="B")
        # NY-close → UTC (20:00 UTC during EDT, 21:00 during EST); pick one for the
        # fixture. The real bug doesn't care which — what matters is *not midnight*.
        idx = idx + pd.Timedelta(hours=21)
        idx = pd.DatetimeIndex(idx, tz="UTC")
        return pd.Series(levels, index=idx, dtype=float)

    def _composite_at_midnight_utc(self, levels: list[float], start: str = "2024-01-01"):
        """Sector composite series — midnight UTC (how sectors/store writes them)."""
        idx = pd.date_range(start, periods=len(levels), freq="B", tz="UTC")
        return pd.Series(levels, index=idx, dtype=float)

    def test_alignment_works_across_midnight_vs_ny_close(self):
        n = 80
        stock_levels = list(np.linspace(100.0, 120.0, n))
        sec_levels = list(np.linspace(100.0, 110.0, n))
        stock = self._stock_at_ny_close(stock_levels)
        sec = self._composite_at_midnight_utc(sec_levels)

        v = relative_strength_values(
            stock, sec,
            look=LOOK, band=BAND, slope_window=SLOPE_W, slope_band=SLOPE_BAND,
        )
        assert v is not None, (
            "stock at NY close + sector at midnight UTC must align by calendar "
            "date — pre-fix this returned None silently (bt20/21 sector_rs bug)."
        )
        # Leader vs sector that rose less — expect positive rs.
        assert v["rs"] > 0

    def test_alignment_is_date_indifferent_to_swapped_intraday(self):
        """Same data, just swap which series has the intraday hour. Result must
        be identical — the indicator should care only about the date, not the time."""
        n = 80
        stock_levels = list(np.linspace(100.0, 120.0, n))
        sec_levels = list(np.linspace(100.0, 110.0, n))

        v1 = relative_strength_values(
            self._stock_at_ny_close(stock_levels),
            self._composite_at_midnight_utc(sec_levels),
            look=LOOK, band=BAND, slope_window=SLOPE_W, slope_band=SLOPE_BAND,
        )
        v2 = relative_strength_values(
            self._composite_at_midnight_utc(stock_levels),
            self._stock_at_ny_close(sec_levels),
            look=LOOK, band=BAND, slope_window=SLOPE_W, slope_band=SLOPE_BAND,
        )
        assert v1 is not None and v2 is not None
        assert v1["rs"] == pytest.approx(v2["rs"])
        assert v1["slope_n"] == pytest.approx(v2["slope_n"])
        assert v1["raw"] == pytest.approx(v2["raw"])


class TestFetchWrapperAbstains:
    def test_no_symbol_abstains(self):
        # A frame with enough history but no resolvable symbol → abstain (None),
        # never raise. (Default rs_window=63, so make it comfortably longer.)
        idx = pd.date_range("2023-01-01", periods=120, freq="B")
        bars = pd.DataFrame({"close": np.linspace(10, 20, 120)}, index=idx)
        assert sector_relative_strength(bars, idx[-1].date()) is None
