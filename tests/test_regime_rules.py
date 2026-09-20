"""Pure regime rules (``stockscan.regime.rules``).

Every function is a trailing window or a forward state machine over a
Series, so each is testable with hand-built inputs and no I/O. The last
class holds the no-look-ahead property the module docstring promises.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from stockscan.regime.rules import (
    TARGET_VOL,
    VOL_HIGH_RANK,
    VOL_SCALAR_FLOOR,
    credit_stress_flag,
    realized_vol,
    regime_frame,
    trend_gate,
    vol_pct_rank,
    vol_scalar,
)

# -----------------------------------------------------------------------
# Trend gate
# -----------------------------------------------------------------------


def _gate(closes: list[float], sma: list[float]):
    idx = pd.date_range("2024-01-01", periods=len(closes), freq="B")
    return trend_gate(pd.Series(closes, index=idx), pd.Series(sma, index=idx))


class TestTrendGate:
    def test_first_valid_bar_seeds_state_from_its_own_side(self):
        is_open, run = _gate([110.0, 110.0], [100.0, 100.0])
        assert is_open.tolist() == [True, True]
        assert run.tolist() == [1, 2]

        is_open, run = _gate([90.0, 90.0], [100.0, 100.0])
        assert is_open.tolist() == [False, False]
        assert run.tolist() == [1, 2]

    def test_bars_before_sma_exists_are_closed_with_zero_counter(self):
        is_open, run = _gate([110.0, 110.0, 110.0], [math.nan, math.nan, 100.0])
        assert is_open.tolist() == [False, False, True]
        assert run.tolist() == [0, 0, 1]

    def test_one_close_through_the_sma_does_not_flip(self):
        closes = [110.0, 110.0, 110.0, 90.0, 110.0, 110.0]
        is_open, run = _gate(closes, [100.0] * len(closes))
        assert is_open.tolist() == [True] * 6
        # The dwell counter resets on each side change.
        assert run.tolist() == [1, 2, 3, 1, 1, 2]

    def test_two_closes_through_the_sma_do_not_flip(self):
        closes = [110.0, 110.0, 90.0, 90.0, 110.0]
        is_open, _ = _gate(closes, [100.0] * len(closes))
        assert is_open.tolist() == [True] * 5

    def test_three_closes_through_the_sma_flip_the_gate(self):
        closes = [110.0, 110.0, 90.0, 90.0, 90.0, 90.0]
        is_open, run = _gate(closes, [100.0] * len(closes))
        assert is_open.tolist() == [True, True, True, True, False, False]
        assert run.tolist() == [1, 2, 1, 2, 3, 4]

    def test_gate_reopens_after_dwell_above(self):
        closes = [90.0] * 3 + [110.0, 110.0, 110.0, 110.0]
        is_open, _ = _gate(closes, [100.0] * len(closes))
        assert is_open.tolist() == [False, False, False, False, False, True, True]

    def test_custom_dwell(self):
        closes = [110.0, 90.0, 90.0]
        idx = pd.date_range("2024-01-01", periods=3, freq="B")
        is_open, _ = trend_gate(
            pd.Series(closes, index=idx), pd.Series([100.0] * 3, index=idx), dwell=1
        )
        assert is_open.tolist() == [True, False, False]

    def test_close_equal_to_sma_counts_as_below(self):
        closes = [110.0, 100.0, 100.0, 100.0]
        is_open, _ = _gate(closes, [100.0] * 4)
        assert is_open.tolist() == [True, True, True, False]

    def test_series_names_and_index_preserved(self):
        idx = pd.date_range("2024-01-01", periods=3, freq="B", tz="UTC")
        is_open, run = trend_gate(
            pd.Series([1.0, 1.0, 1.0], index=idx), pd.Series([0.5] * 3, index=idx)
        )
        assert is_open.name == "trend_gate_open"
        assert run.name == "days_on_side"
        assert is_open.index.equals(idx)
        assert is_open.dtype == bool


# -----------------------------------------------------------------------
# Volatility scalar
# -----------------------------------------------------------------------


def _alternating_close(n: int, r: float = 0.02, start: float = 100.0) -> pd.Series:
    """Closes whose log returns alternate +r / -r exactly."""
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    steps = np.array([r if i % 2 == 0 else -r for i in range(n - 1)])
    log_px = math.log(start) + np.concatenate([[0.0], np.cumsum(steps)])
    return pd.Series(np.exp(log_px), index=idx)


class TestRealizedVol:
    def test_warmup_is_nan_then_defined(self):
        close = _alternating_close(40)
        rv = realized_vol(close, window=20)
        assert rv.iloc[:20].isna().all()
        assert rv.iloc[20:].notna().all()
        assert rv.name == "realized_vol"

    def test_alternating_returns_give_exact_annualized_std(self):
        # Even window → mean of ±r is 0 → population std is exactly r.
        close = _alternating_close(40, r=0.02)
        rv = realized_vol(close, window=20)
        assert rv.iloc[-1] == pytest.approx(0.02 * math.sqrt(252), rel=1e-9)

    def test_constant_price_has_zero_vol(self):
        idx = pd.date_range("2024-01-01", periods=30, freq="B")
        rv = realized_vol(pd.Series(100.0, index=idx), window=20)
        assert rv.iloc[-1] == pytest.approx(0.0)


class TestVolPctRank:
    def test_warmup_nan_and_highest_ranks_one(self):
        idx = pd.date_range("2024-01-01", periods=12, freq="B")
        rv = pd.Series(np.arange(12, dtype=float), index=idx)
        rank = vol_pct_rank(rv, window=10)
        assert rank.iloc[:9].isna().all()
        assert rank.iloc[9:].tolist() == [1.0, 1.0, 1.0]
        assert rank.name == "vol_pct_rank"

    def test_lowest_in_window_ranks_bottom(self):
        idx = pd.date_range("2024-01-01", periods=10, freq="B")
        rv = pd.Series(np.arange(10, 0, -1, dtype=float), index=idx)
        rank = vol_pct_rank(rv, window=10)
        assert rank.iloc[-1] == pytest.approx(0.1)


class TestVolScalar:
    def _scalar(self, rv: list[float], rank: list[float]) -> pd.Series:
        idx = pd.date_range("2024-01-01", periods=len(rv), freq="B")
        return vol_scalar(pd.Series(rv, index=idx), pd.Series(rank, index=idx))

    def test_dead_band_below_top_tercile_is_one(self):
        s = self._scalar([0.40, 0.60], [0.5, 0.66])
        assert s.tolist() == [1.0, 1.0]

    def test_top_tercile_scales_target_over_realized(self):
        s = self._scalar([0.20, 0.32], [0.9, 1.0])
        assert s.iloc[0] == pytest.approx(TARGET_VOL / 0.20)  # 0.8
        assert s.iloc[1] == pytest.approx(VOL_SCALAR_FLOOR)  # exactly at the floor

    def test_rank_exactly_at_threshold_is_active(self):
        s = self._scalar([0.20], [VOL_HIGH_RANK])
        assert s.iloc[0] == pytest.approx(0.8)

    def test_clipped_to_floor_and_never_above_one(self):
        s = self._scalar([0.80, 0.10], [0.9, 0.9])
        assert s.iloc[0] == VOL_SCALAR_FLOOR
        assert s.iloc[1] == 1.0  # calm-but-top-ranked never levers up

    def test_nan_inputs_give_nan(self):
        s = self._scalar([math.nan, 0.20, 0.20], [0.9, math.nan, 0.9])
        assert math.isnan(s.iloc[0])
        assert math.isnan(s.iloc[1])
        assert s.iloc[2] == pytest.approx(0.8)
        assert s.name == "vol_scalar"

    def test_nan_in_warmup_of_full_pipeline(self):
        close = _alternating_close(300)
        rv = realized_vol(close)
        s = vol_scalar(rv, vol_pct_rank(rv))
        # 20 bars of vol warmup + 252 bars of rank warmup.
        assert s.iloc[: 20 + 251].isna().all()
        assert s.iloc[20 + 251 :].notna().all()


# -----------------------------------------------------------------------
# Credit-stress flag
# -----------------------------------------------------------------------


def _oas(values: list[float]) -> pd.Series:
    idx = pd.date_range("2024-01-01", periods=len(values), freq="B")
    return pd.Series(values, index=idx)


class TestCreditStressFlag:
    def test_warmup_is_false_not_nan(self):
        flag = credit_stress_flag(_oas([3.0] * 5), window=10, lookback=2)
        assert flag.dtype == bool
        assert not flag.any()
        assert flag.name == "credit_stress_flag"

    def test_high_rank_and_rising_fires(self):
        values = [3.0] * 10 + [3.5, 4.0, 4.5]
        flag = credit_stress_flag(_oas(values), window=10, lookback=2)
        # 4.5 is the max of its window and > the value 2 bars back (3.5).
        assert bool(flag.iloc[-1]) is True

    def test_high_rank_but_flat_does_not_fire(self):
        values = [3.0] * 10 + [5.0, 5.0, 5.0, 5.0]
        flag = credit_stress_flag(_oas(values), window=10, lookback=2)
        assert bool(flag.iloc[-1]) is False  # plateau: not rising

    def test_rising_but_low_rank_does_not_fire(self):
        values = [6.0, 5.5, 5.0, 4.5, 4.0, 3.5, 3.0, 2.5, 2.0, 1.5, 1.6, 1.7]
        flag = credit_stress_flag(_oas(values), window=10, lookback=2)
        assert bool(flag.iloc[-1]) is False

    def test_rank_threshold_is_strict(self):
        # 10-bar window: the max ranks 1.0; the second-highest ranks 0.9.
        values = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 10.0, 9.0]
        flag = credit_stress_flag(_oas(values), window=10, lookback=2, rank_threshold=0.9)
        assert bool(flag.iloc[-1]) is False


# -----------------------------------------------------------------------
# regime_frame
# -----------------------------------------------------------------------

_FRAME_COLUMNS = {
    "sma200",
    "sma200_slope_20d",
    "trend_gate_open",
    "days_on_side",
    "realized_vol",
    "vol_pct_rank",
    "vol_scalar",
    "hy_oas",
    "hy_oas_pct_rank",
    "credit_stress_flag",
}


def _spy_close(n: int, *, tz: str | None = "UTC", seed: int = 7) -> pd.Series:
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2022-01-03 21:00", periods=n, freq="B", tz=tz)
    log_ret = rng.normal(0.0004, 0.01, size=n)
    # A volatility burst in the second half so the scalar leaves the dead band.
    log_ret[int(n * 0.7) : int(n * 0.7) + 40] *= 4.0
    close = 400.0 * np.exp(np.cumsum(log_ret))
    return pd.Series(close, index=idx, name="close")


class TestRegimeFrame:
    def test_columns_and_index(self):
        close = _spy_close(300)
        frame = regime_frame(close, None)
        assert set(frame.columns) == _FRAME_COLUMNS
        assert frame.index.equals(close.index)
        assert len(frame) == 300

    def test_no_hy_oas_gives_false_flag_and_nan_levels(self):
        frame = regime_frame(_spy_close(260), None)
        assert frame["credit_stress_flag"].dtype == bool
        assert not frame["credit_stress_flag"].any()
        assert frame["hy_oas"].isna().all()
        assert frame["hy_oas_pct_rank"].isna().all()

    def test_empty_hy_oas_treated_as_missing(self):
        frame = regime_frame(_spy_close(260), pd.Series(dtype=float))
        assert not frame["credit_stress_flag"].any()
        assert frame["hy_oas"].isna().all()

    def test_gate_and_scalar_follow_the_pure_rules(self):
        close = _spy_close(400)
        frame = regime_frame(close, None)
        sma = close.rolling(200, min_periods=200).mean()
        expected_open, expected_run = trend_gate(close, sma)
        assert frame["trend_gate_open"].tolist() == expected_open.tolist()
        assert frame["days_on_side"].tolist() == expected_run.tolist()
        assert frame["sma200"].iloc[:199].isna().all()
        rv = realized_vol(close)
        pd.testing.assert_series_equal(
            frame["vol_scalar"], vol_scalar(rv, vol_pct_rank(rv)), check_names=False
        )

    def test_sma_slope_is_20d_relative_change(self):
        close = _spy_close(300)
        frame = regime_frame(close, None)
        sma = frame["sma200"]
        expected = (sma.iloc[-1] - sma.iloc[-21]) / sma.iloc[-21]
        assert frame["sma200_slope_20d"].iloc[-1] == pytest.approx(expected)

    def test_hy_oas_aligned_by_date_onto_tz_aware_spy_bars_with_ffill(self):
        close = _spy_close(30, tz="UTC")
        bar_dates = [ts.date() for ts in close.index]
        # FRED prints on business days, with the 3rd and 4th bars missing.
        oas_dates = [d for i, d in enumerate(bar_dates) if i not in (2, 3)]
        oas = pd.Series(
            [4.0 + 0.1 * i for i in range(len(oas_dates))],
            index=pd.DatetimeIndex(pd.to_datetime(oas_dates)),  # naive dates
        )
        frame = regime_frame(close, oas)
        aligned = frame["hy_oas"]
        assert aligned.index.equals(close.index)
        assert aligned.iloc[0] == pytest.approx(4.0)
        assert aligned.iloc[1] == pytest.approx(4.1)
        # Missing prints take the latest observation on or before the bar.
        assert aligned.iloc[2] == pytest.approx(4.1)
        assert aligned.iloc[3] == pytest.approx(4.1)
        assert aligned.iloc[4] == pytest.approx(4.2)
        assert aligned.iloc[-1] == pytest.approx(oas.iloc[-1])
        assert frame["credit_stress_flag"].dtype == bool

    def test_oas_before_first_bar_leaves_nan_and_false(self):
        close = _spy_close(10)
        # Every OAS print is AFTER the last SPY bar → nothing to ffill from.
        oas = pd.Series(
            [4.0, 4.1],
            index=pd.date_range(close.index[-1].date() + pd.Timedelta(days=5), periods=2, freq="B"),
        )
        frame = regime_frame(close, oas)
        assert frame["hy_oas"].isna().all()
        assert not frame["credit_stress_flag"].any()

    def test_credit_stress_flag_fires_on_aligned_series(self):
        close = _spy_close(300, tz="UTC")
        bar_dates = pd.DatetimeIndex([ts.date() for ts in close.index])
        values = np.full(300, 3.5)
        values[-10:] = np.linspace(4.0, 6.0, 10)  # spike into the top of the year, rising
        oas = pd.Series(values, index=bar_dates)
        frame = regime_frame(close, oas)
        assert bool(frame["credit_stress_flag"].iloc[-1]) is True
        assert frame["hy_oas_pct_rank"].iloc[-1] == pytest.approx(1.0)
        assert not frame["credit_stress_flag"].iloc[:-10].any()


# -----------------------------------------------------------------------
# No look-ahead
# -----------------------------------------------------------------------


class TestNoLookAhead:
    """Recomputing on a truncated copy of the input matches the live value
    at the truncation point. The live detector reads ``iloc[-1]`` of a
    truncated series; the backtest reads row ``k-1`` of the full frame.
    They must agree or the backtest measures something live never sees."""

    N = 700

    @pytest.fixture(scope="class")
    def full(self):
        close = _spy_close(self.N, tz="UTC", seed=11)
        bar_dates = pd.DatetimeIndex([ts.date() for ts in close.index])
        rng = np.random.default_rng(3)
        oas_vals = 3.5 + np.cumsum(rng.normal(0.0, 0.05, size=self.N))
        oas_vals[-60:] += np.linspace(0.0, 2.5, 60)  # a stress episode at the end
        oas = pd.Series(oas_vals, index=bar_dates)
        return close, oas, regime_frame(close, oas)

    @pytest.mark.parametrize("k", [280, 350, 430, 520, 660, 700])
    def test_truncated_last_row_equals_full_row(self, full, k):
        close, oas, frame = full
        cutoff = close.index[k - 1].date()
        truncated = regime_frame(close.iloc[:k], oas[oas.index.date <= cutoff])
        live = truncated.iloc[-1]
        hist = frame.iloc[k - 1]

        assert bool(live["trend_gate_open"]) == bool(hist["trend_gate_open"])
        assert int(live["days_on_side"]) == int(hist["days_on_side"])
        assert bool(live["credit_stress_flag"]) == bool(hist["credit_stress_flag"])
        for col in ("sma200", "vol_scalar", "realized_vol", "vol_pct_rank", "hy_oas_pct_rank"):
            a, b = live[col], hist[col]
            if pd.isna(a) or pd.isna(b):
                assert pd.isna(a) and pd.isna(b), col
            else:
                assert a == pytest.approx(b, rel=1e-12), col

    def test_synthetic_series_exercises_every_control(self, full):
        # Guard against a fixture that never flips anything, which would
        # make the property above vacuous.
        _, _, frame = full
        assert frame["trend_gate_open"].iloc[200:].any()
        assert (frame["vol_scalar"].dropna() < 1.0).any()
        assert frame["credit_stress_flag"].any()
