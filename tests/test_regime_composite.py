"""Unit + property tests for the v2 regime composite math.

The single most important thing this file enforces is the **no-look-ahead
property**: any function that uses a rolling window must produce the
same value at index ``t`` whether you compute it on the full series or
on the truncated prefix ``series[:t+1]``. The research doc §5.1 and §7.2
are emphatic that this is the failure mode that destroys regime
backtests, so the property test is intentionally exhaustive across each
windowed function.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from stockscan.regime.composite import (
    DEFAULT_WEIGHTS,
    breadth_score,
    composite_score,
    composite_score_series,
    credit_score,
    credit_stress_flag,
    hy_oas_zscore,
    trend_score,
    vol_score,
)


# ======================================================================
# No-look-ahead property — the crown-jewel invariant
# ======================================================================
class TestNoLookAhead:
    """Recomputing any windowed function on a truncated prefix must match
    the live value at that truncation point."""

    @pytest.fixture
    def vix_synthetic(self) -> pd.Series:
        rng = np.random.default_rng(42)
        # 500 trading days of plausible VIX values, drift + AR(1) noise.
        n = 500
        idx = pd.date_range("2024-01-01", periods=n, freq="B")
        base = 18.0 + 4.0 * np.sin(np.linspace(0.0, 12.0, n))
        noise = rng.normal(0, 1.0, n).cumsum() * 0.1
        vix = pd.Series(np.clip(base + noise, 9.0, 60.0), index=idx, name="vix")
        return vix

    @pytest.fixture
    def hy_oas_synthetic(self) -> pd.Series:
        rng = np.random.default_rng(7)
        n = 500
        idx = pd.date_range("2024-01-01", periods=n, freq="B")
        # HY OAS in roughly 3% to 8% range, slow-moving.
        levels = 4.0 + np.cumsum(rng.normal(0, 0.05, n))
        return pd.Series(np.clip(levels, 2.5, 12.0), index=idx, name="hy_oas")

    @pytest.mark.parametrize("truncate_at", [260, 300, 400, 499])
    def test_vol_score_is_truncation_invariant(self, vix_synthetic, truncate_at):
        full = vol_score(vix_synthetic)
        truncated = vol_score(vix_synthetic.iloc[: truncate_at + 1])
        # The score at the last bar of the truncated input must equal the
        # score at the same bar in the full computation.
        assert truncated.iloc[-1] == pytest.approx(full.iloc[truncate_at], rel=1e-12)

    @pytest.mark.parametrize("truncate_at", [260, 300, 400, 499])
    def test_credit_score_is_truncation_invariant(self, hy_oas_synthetic, truncate_at):
        full = credit_score(hy_oas_synthetic)
        truncated = credit_score(hy_oas_synthetic.iloc[: truncate_at + 1])
        assert truncated.iloc[-1] == pytest.approx(full.iloc[truncate_at], rel=1e-12)

    @pytest.mark.parametrize("truncate_at", [260, 300, 400, 499])
    def test_hy_oas_zscore_is_truncation_invariant(self, hy_oas_synthetic, truncate_at):
        full = hy_oas_zscore(hy_oas_synthetic)
        truncated = hy_oas_zscore(hy_oas_synthetic.iloc[: truncate_at + 1])
        assert truncated.iloc[-1] == pytest.approx(full.iloc[truncate_at], rel=1e-12)

    @pytest.mark.parametrize("truncate_at", [260, 300, 400, 499])
    def test_credit_stress_flag_is_truncation_invariant(self, hy_oas_synthetic, truncate_at):
        full = credit_stress_flag(hy_oas_synthetic)
        truncated = credit_stress_flag(hy_oas_synthetic.iloc[: truncate_at + 1])
        assert bool(truncated.iloc[-1]) == bool(full.iloc[truncate_at])

    @pytest.mark.parametrize("truncate_at", [260, 350, 499])
    def test_composite_pipeline_is_truncation_invariant(
        self, vix_synthetic, hy_oas_synthetic, truncate_at
    ):
        """End-to-end: feed truncated series through every component +
        the vectorized composite. The composite at the last bar of the
        truncated input must equal the full-series composite at the same
        index. This is the integration counterpart of the per-component
        property tests above."""
        # Synthetic SPY-like inputs aligned to the same index as VIX, so
        # all four components share a trading-day timeline.
        n = len(vix_synthetic)
        rng = np.random.default_rng(11)
        spy_close = pd.Series(
            350.0 + np.cumsum(rng.normal(0, 1.0, n)) * 0.4,
            index=vix_synthetic.index,
            name="spy",
        )
        rsp_close = pd.Series(
            150.0 + np.cumsum(rng.normal(0, 1.0, n)) * 0.2,
            index=vix_synthetic.index,
            name="rsp",
        )
        sma200 = spy_close.rolling(200, min_periods=200).mean()

        def _pipeline(
            vix: pd.Series,
            hy: pd.Series,
            spy: pd.Series,
            rsp: pd.Series,
        ) -> pd.Series:
            sma = spy.rolling(200, min_periods=200).mean()
            return composite_score_series(
                vol_score(vix),
                trend_score(spy, sma),
                breadth_score(rsp, spy),
                credit_score(hy),
            )

        full = _pipeline(vix_synthetic, hy_oas_synthetic, spy_close, rsp_close)
        truncated = _pipeline(
            vix_synthetic.iloc[: truncate_at + 1],
            hy_oas_synthetic.iloc[: truncate_at + 1],
            spy_close.iloc[: truncate_at + 1],
            rsp_close.iloc[: truncate_at + 1],
        )
        # `sma200` not actually used directly here — it's recomputed inside
        # _pipeline to ensure the truncated path uses only truncated history.
        del sma200
        live = full.iloc[truncate_at]
        replay = truncated.iloc[-1]
        if math.isnan(live):
            assert math.isnan(replay)
        else:
            assert replay == pytest.approx(live, rel=1e-12, abs=1e-12)


# ======================================================================
# Vol score
# ======================================================================
class TestVolScore:
    def test_warmup_returns_nan(self):
        s = pd.Series(np.random.default_rng(1).uniform(10, 30, 100))
        out = vol_score(s, window=50)
        # First window-1 bars are NaN.
        assert out.iloc[:49].isna().all()
        assert not math.isnan(out.iloc[49])

    def test_low_vix_maps_to_high_score(self):
        # A series where VIX rises smoothly: the LAST bar is the highest,
        # so its rank is 1.0 -> score is 0.0. The FIRST bar after warmup
        # is the lowest -> rank 1/window, score ≈ 1.
        s = pd.Series(np.linspace(10.0, 30.0, 252))
        out = vol_score(s, window=252)
        assert out.iloc[-1] == pytest.approx(0.0, abs=1e-10)

    def test_high_vix_maps_to_low_score(self):
        # Inverse: descending series, last bar is the lowest -> rank 0,
        # so score = 1.0.
        s = pd.Series(np.linspace(30.0, 10.0, 252))
        out = vol_score(s, window=252)
        assert out.iloc[-1] == pytest.approx(1.0 - 1.0 / 252, abs=1e-10)

    def test_score_is_in_unit_interval(self):
        s = pd.Series(np.random.default_rng(2).uniform(9, 60, 500))
        out = vol_score(s, window=252).dropna()
        assert (out >= 0.0).all()
        assert (out <= 1.0).all()


# ======================================================================
# Trend score
# ======================================================================
class TestTrendScore:
    def test_strong_uptrend_close_above_sma_with_rising_sma(self):
        # close 5%+ above sma200, sma200 trending up -> score ≈ 1.0.
        idx = pd.date_range("2024-01-01", periods=100, freq="B")
        sma = pd.Series(np.linspace(100.0, 110.0, 100), index=idx)  # rising
        close = sma * 1.10  # 10% above (saturates at 5% band)
        out = trend_score(close, sma)
        assert out.iloc[-1] == pytest.approx(1.0, abs=1e-10)

    def test_strong_downtrend(self):
        idx = pd.date_range("2024-01-01", periods=100, freq="B")
        sma = pd.Series(np.linspace(110.0, 100.0, 100), index=idx)  # falling
        close = sma * 0.90  # 10% below
        out = trend_score(close, sma)
        assert out.iloc[-1] == pytest.approx(0.0, abs=1e-10)

    def test_neutral_close_at_sma_flat_slope(self):
        # close == sma, sma flat -> 0.5.
        idx = pd.date_range("2024-01-01", periods=100, freq="B")
        sma = pd.Series([100.0] * 100, index=idx)
        close = sma.copy()
        out = trend_score(close, sma)
        assert out.iloc[-1] == pytest.approx(0.5, abs=1e-10)

    def test_score_in_unit_interval(self):
        rng = np.random.default_rng(3)
        idx = pd.date_range("2024-01-01", periods=300, freq="B")
        sma = pd.Series(100.0 + rng.normal(0, 5, 300).cumsum() * 0.1, index=idx)
        close = sma * (1 + rng.normal(0, 0.02, 300))
        out = trend_score(close, sma).dropna()
        assert (out >= 0.0).all()
        assert (out <= 1.0).all()


# ======================================================================
# Breadth score (RSP/SPY proxy)
# ======================================================================
class TestBreadthScore:
    def test_rising_rsp_relative_to_spy_is_high_score(self):
        # RSP rising 10% over the period, SPY flat: 20d ratio > 200d ratio,
        # so breadth healthy -> score above 0.5.
        idx = pd.date_range("2024-01-01", periods=300, freq="B")
        spy = pd.Series([100.0] * 300, index=idx)
        rsp = pd.Series(np.linspace(100.0, 110.0, 300), index=idx)
        out = breadth_score(rsp, spy)
        assert out.iloc[-1] > 0.7

    def test_falling_rsp_relative_to_spy_is_low_score(self):
        # Concentration regime: RSP falling 10%, SPY flat. Score should drop.
        idx = pd.date_range("2024-01-01", periods=300, freq="B")
        spy = pd.Series([100.0] * 300, index=idx)
        rsp = pd.Series(np.linspace(110.0, 100.0, 300), index=idx)
        out = breadth_score(rsp, spy)
        assert out.iloc[-1] < 0.3

    def test_score_in_unit_interval(self):
        rng = np.random.default_rng(4)
        idx = pd.date_range("2024-01-01", periods=300, freq="B")
        spy = pd.Series(100.0 + rng.normal(0, 1, 300).cumsum() * 0.05, index=idx)
        rsp = pd.Series(100.0 + rng.normal(0, 1, 300).cumsum() * 0.05, index=idx)
        out = breadth_score(rsp, spy).dropna()
        assert (out >= 0.0).all()
        assert (out <= 1.0).all()


# ======================================================================
# Credit score + stress flag
# ======================================================================
class TestCreditScore:
    def test_tight_spreads_high_score(self):
        # Final value is the lowest in the window -> rank 0 -> score 1.
        s = pd.Series(np.linspace(8.0, 3.0, 252))
        out = credit_score(s, window=252)
        assert out.iloc[-1] == pytest.approx(1.0 - 1.0 / 252, abs=1e-10)

    def test_wide_spreads_low_score(self):
        s = pd.Series(np.linspace(3.0, 8.0, 252))
        out = credit_score(s, window=252)
        assert out.iloc[-1] == pytest.approx(0.0, abs=1e-10)


class TestCreditStressFlag:
    def test_rank_above_threshold_and_rising_fires_flag(self):
        # 252 days. Most of the year HY OAS is moderate (3-5).
        # Last 10 days: spike to 9 (top of the window) AND rising.
        n = 252
        oas = np.concatenate([np.full(n - 10, 4.0), np.linspace(4.0, 9.0, 10)])
        s = pd.Series(oas)
        flag = credit_stress_flag(s)
        # Last bar: rank near top, rising over last 5 days -> True.
        assert bool(flag.iloc[-1]) is True

    def test_rank_below_threshold_no_flag(self):
        # Steady 4.0 the whole year — rank ~0.5, no stress.
        s = pd.Series([4.0] * 252)
        flag = credit_stress_flag(s)
        assert bool(flag.iloc[-1]) is False

    def test_high_rank_but_falling_does_not_fire(self):
        # Spread was high recently but is now falling -> not stress.
        # Start moderate, jump to 9, then drift back down.
        n = 252
        oas = np.concatenate([np.full(n - 20, 4.0), np.full(10, 9.0), np.linspace(9.0, 5.0, 10)])
        s = pd.Series(oas)
        flag = credit_stress_flag(s)
        # rank still high but rising-test fails (today < value 5 days ago).
        assert bool(flag.iloc[-1]) is False

    def test_warmup_returns_false_not_nan(self):
        s = pd.Series([4.0] * 100)
        flag = credit_stress_flag(s, window=252)
        assert flag.dtype == bool
        assert not flag.any()


# ======================================================================
# HY OAS z-score
# ======================================================================
class TestHyOasZscore:
    def test_value_at_window_mean_is_zero(self):
        # Constant 4.0 for window-1 days, then 4.0 again — z=0.
        # But std(constant)=0 so we'd divide by zero. So use slight noise
        # then verify the LAST bar (which equals the mean) gets z near 0.
        rng = np.random.default_rng(5)
        n = 252
        s = pd.Series(4.0 + rng.normal(0, 0.5, n))
        s.iloc[-1] = float(s.iloc[:-1].mean())  # last bar exactly at trailing mean
        # Recompute mean over the FULL window (which now includes last).
        z = hy_oas_zscore(s)
        # The exact value depends on whether trailing window includes the
        # current bar. Our impl includes it. Confirm |z| is small.
        assert abs(z.iloc[-1]) < 0.5

    def test_extreme_value_has_large_zscore(self):
        rng = np.random.default_rng(6)
        n = 252
        s = pd.Series(4.0 + rng.normal(0, 0.2, n))
        s.iloc[-1] = 10.0  # extreme
        z = hy_oas_zscore(s)
        assert z.iloc[-1] > 5.0


# ======================================================================
# Composite — scalar
# ======================================================================
class TestCompositeScalar:
    def test_all_components_present_uses_research_doc_weights(self):
        # All ones -> 1.0. All zeros -> 0.0.
        assert composite_score(1.0, 1.0, 1.0, 1.0) == pytest.approx(1.0)
        assert composite_score(0.0, 0.0, 0.0, 0.0) == pytest.approx(0.0)

    def test_known_weighted_average(self):
        # Weights are (0.40, 0.25, 0.20, 0.15). All distinct components.
        v = composite_score(1.0, 0.0, 1.0, 0.0)
        # 0.40*1 + 0.25*0 + 0.20*1 + 0.15*0 = 0.60
        assert v == pytest.approx(0.60)

    def test_one_missing_component_renormalizes(self):
        # credit=None: total weight = 0.40 + 0.25 + 0.20 = 0.85.
        # 0.40*1 + 0.25*0 + 0.20*1 + 0*credit / 0.85 = 0.60 / 0.85 ≈ 0.7059.
        v = composite_score(1.0, 0.0, 1.0, None)
        assert v == pytest.approx(0.60 / 0.85, abs=1e-9)

    def test_nan_treated_like_none(self):
        a = composite_score(1.0, 0.0, 1.0, None)
        b = composite_score(1.0, 0.0, 1.0, float("nan"))
        assert a == pytest.approx(b)  # type: ignore[arg-type]

    def test_all_missing_returns_none(self):
        assert composite_score(None, None, None, None) is None
        assert composite_score(float("nan"), float("nan"), float("nan"), float("nan")) is None

    def test_default_weights_match_research_doc(self):
        assert DEFAULT_WEIGHTS == (0.40, 0.25, 0.20, 0.15)


# ======================================================================
# Composite — Series (vectorized replay)
# ======================================================================
class TestCompositeSeries:
    def test_scalar_and_series_results_agree(self):
        rng = np.random.default_rng(7)
        idx = pd.date_range("2024-01-01", periods=10, freq="B")
        vol = pd.Series(rng.uniform(0, 1, 10), index=idx)
        trend = pd.Series(rng.uniform(0, 1, 10), index=idx)
        breadth = pd.Series(rng.uniform(0, 1, 10), index=idx)
        credit = pd.Series(rng.uniform(0, 1, 10), index=idx)
        out = composite_score_series(vol, trend, breadth, credit)
        for i in range(10):
            scalar = composite_score(
                float(vol.iloc[i]),
                float(trend.iloc[i]),
                float(breadth.iloc[i]),
                float(credit.iloc[i]),
            )
            assert out.iloc[i] == pytest.approx(scalar)  # type: ignore[arg-type]

    def test_per_row_renormalization_when_credit_is_nan(self):
        idx = pd.date_range("2024-01-01", periods=3, freq="B")
        vol = pd.Series([1.0, 1.0, 1.0], index=idx)
        trend = pd.Series([0.0, 0.0, 0.0], index=idx)
        breadth = pd.Series([1.0, 1.0, 1.0], index=idx)
        credit = pd.Series([float("nan"), 0.0, 1.0], index=idx)
        out = composite_score_series(vol, trend, breadth, credit)
        # Row 0: credit NaN, renormalize. (0.4 + 0 + 0.2) / 0.85 = 0.7059
        assert out.iloc[0] == pytest.approx(0.60 / 0.85, abs=1e-9)
        # Row 1: full weights. 0.4 + 0 + 0.2 + 0 = 0.60
        assert out.iloc[1] == pytest.approx(0.60, abs=1e-9)
        # Row 2: full weights. 0.4 + 0 + 0.2 + 0.15 = 0.75
        assert out.iloc[2] == pytest.approx(0.75, abs=1e-9)

    def test_all_nan_row_yields_nan(self):
        idx = pd.date_range("2024-01-01", periods=2, freq="B")
        vol = pd.Series([float("nan"), 1.0], index=idx)
        trend = pd.Series([float("nan"), 0.0], index=idx)
        breadth = pd.Series([float("nan"), 1.0], index=idx)
        credit = pd.Series([float("nan"), 0.0], index=idx)
        out = composite_score_series(vol, trend, breadth, credit)
        assert math.isnan(out.iloc[0])
        assert not math.isnan(out.iloc[1])
