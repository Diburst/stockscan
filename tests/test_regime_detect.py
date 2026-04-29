"""Unit tests for the market-regime detector.

All tests are pure (no DB required): we test classify_regime directly
and stub out get_bars / upsert_regime for detect_regime.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import patch

import numpy as np
import pandas as pd

from stockscan.regime.detect import (
    _ADX_CHOP_THRESHOLD,
    _ADX_TREND_THRESHOLD,
    classify_regime,
    detect_regime,
)
from stockscan.regime.store import MarketRegime

# -----------------------------------------------------------------------
# classify_regime — pure function, no I/O
# -----------------------------------------------------------------------


class TestClassifyRegime:
    def test_trending_up(self):
        # ADX > 25, close above SMA200
        assert classify_regime(30.0, 450.0, 400.0) == "trending_up"

    def test_trending_down(self):
        # ADX > 25, close below SMA200
        assert classify_regime(30.0, 350.0, 400.0) == "trending_down"

    def test_choppy(self):
        # ADX < 18 — range-bound
        assert classify_regime(12.0, 450.0, 400.0) == "choppy"
        assert classify_regime(12.0, 350.0, 400.0) == "choppy"

    def test_transitioning(self):
        # ADX in the 18-25 ambiguous band
        assert classify_regime(20.0, 450.0, 400.0) == "transitioning"
        assert classify_regime(20.0, 350.0, 400.0) == "transitioning"

    def test_adx_exactly_at_trend_threshold_is_not_trending(self):
        # Boundary: ADX == 25 is NOT > 25, so transitioning.
        assert classify_regime(_ADX_TREND_THRESHOLD, 450.0, 400.0) == "transitioning"

    def test_adx_exactly_at_chop_threshold_is_not_choppy(self):
        # Boundary: ADX == 18 is NOT < 18, so transitioning.
        assert classify_regime(_ADX_CHOP_THRESHOLD, 450.0, 400.0) == "transitioning"

    def test_adx_just_above_trend_threshold(self):
        assert classify_regime(_ADX_TREND_THRESHOLD + 0.01, 450.0, 400.0) == "trending_up"

    def test_adx_just_below_chop_threshold(self):
        assert classify_regime(_ADX_CHOP_THRESHOLD - 0.01, 450.0, 400.0) == "choppy"

    def test_close_equal_to_sma200_goes_to_trending_down(self):
        # close == sma200: not strictly greater, so trending_down.
        assert classify_regime(30.0, 400.0, 400.0) == "trending_down"


# -----------------------------------------------------------------------
# detect_regime — integration wrapper (stubbed I/O)
# -----------------------------------------------------------------------


def _make_spy_bars(n: int = 250) -> pd.DataFrame:
    """Synthetic steadily-rising SPY bars — produces ADX > 25, close > SMA200."""
    idx = pd.date_range("2023-01-01", periods=n, freq="B")
    close = pd.Series(np.linspace(350.0, 500.0, n), index=idx)
    high = close + 2.0
    low = close - 2.0
    return pd.DataFrame({"open": close, "high": high, "low": low, "close": close})


class TestDetectRegime:
    def test_returns_cached_result_without_fetching_bars(self):
        # v2 cache contract: only rows with methodology_version >= 2 are
        # treated as fresh; v1 rows get re-detected on next call so the
        # composite columns can backfill.
        cached = MarketRegime(
            as_of_date=date(2026, 4, 28),
            regime="trending_up",
            adx=Decimal("28.5"),
            spy_close=Decimal("520.0"),
            spy_sma200=Decimal("480.0"),
            methodology_version=2,
        )
        with (
            patch("stockscan.regime.detect.get_regime", return_value=cached) as mock_get,
            patch("stockscan.regime.detect.get_bars") as mock_bars,
        ):
            result = detect_regime(date(2026, 4, 28))
        assert result is cached
        mock_get.assert_called_once()
        mock_bars.assert_not_called()

    def test_computes_and_stores_when_not_cached(self):
        bars = _make_spy_bars(250)
        stored: list[MarketRegime] = []

        # v2 upsert_regime takes a wider kwarg set; absorb the extras.
        def fake_upsert(as_of, regime, *, adx, spy_close, spy_sma200, session=None, **_kw):
            mr = MarketRegime(
                as_of_date=as_of,
                regime=regime,
                adx=Decimal(str(round(adx, 2))),
                spy_close=Decimal(str(round(spy_close, 4))),
                spy_sma200=Decimal(str(round(spy_sma200, 4))),
                methodology_version=_kw.get("methodology_version", 1),
            )
            stored.append(mr)
            return mr

        empty = pd.Series(dtype=float)
        with (
            patch("stockscan.regime.detect.get_regime", return_value=None),
            patch("stockscan.regime.detect.get_bars", return_value=bars),
            patch("stockscan.regime.detect.get_macro_series", return_value=empty),
            patch("stockscan.regime.detect.upsert_regime", side_effect=fake_upsert),
        ):
            result = detect_regime(date(2026, 4, 28))

        assert result is not None
        assert result.regime in {"trending_up", "trending_down", "choppy", "transitioning"}
        assert len(stored) == 1
        # v2 detector stamps methodology_version=2 on every fresh row.
        assert stored[0].methodology_version == 2

    def test_returns_none_when_bars_unavailable(self):
        with (
            patch("stockscan.regime.detect.get_regime", return_value=None),
            patch("stockscan.regime.detect.get_bars", return_value=pd.DataFrame()),
        ):
            result = detect_regime(date(2026, 4, 28))
        assert result is None

    def test_returns_none_when_bars_fetch_raises(self):
        with (
            patch("stockscan.regime.detect.get_regime", return_value=None),
            patch("stockscan.regime.detect.get_bars", side_effect=Exception("no table")),
        ):
            result = detect_regime(date(2026, 4, 28))
        assert result is None

    def test_returns_none_when_insufficient_bars(self):
        sparse = _make_spy_bars(n=50)  # far fewer than _MIN_LEGACY_BARS
        with (
            patch("stockscan.regime.detect.get_regime", return_value=None),
            patch("stockscan.regime.detect.get_bars", return_value=sparse),
        ):
            result = detect_regime(date(2026, 4, 28))
        assert result is None

    def test_no_lookahead_bias(self):
        """Bars after as_of must not influence the detected regime."""
        bars = _make_spy_bars(300)
        cutoff = date(2024, 1, 15)

        captured_bars: list[pd.DataFrame] = []

        def fake_upsert(as_of, regime, *, adx, spy_close, spy_sma200, session=None, **_kw):
            return MarketRegime(
                as_of_date=as_of,
                regime=regime,
                adx=Decimal(str(round(adx, 2))),
                spy_close=Decimal(str(round(spy_close, 4))),
                spy_sma200=Decimal(str(round(spy_sma200, 4))),
            )

        original_adx = __import__("stockscan.indicators", fromlist=["adx"]).adx

        def capturing_adx(high, low, close, period=14):
            captured_bars.append(close)
            return original_adx(high, low, close, period)

        empty = pd.Series(dtype=float)
        with (
            patch("stockscan.regime.detect.get_regime", return_value=None),
            patch("stockscan.regime.detect.get_bars", return_value=bars),
            patch("stockscan.regime.detect.get_macro_series", return_value=empty),
            patch("stockscan.regime.detect.upsert_regime", side_effect=fake_upsert),
            patch("stockscan.regime.detect.compute_adx", side_effect=capturing_adx),
        ):
            detect_regime(cutoff)

        if captured_bars:
            latest_idx = captured_bars[0].index[-1]
            assert latest_idx.date() <= cutoff
