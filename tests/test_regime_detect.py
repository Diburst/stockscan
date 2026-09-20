"""Market-regime detector (``stockscan.regime.detect``) and the stored row.

No database: ``get_bars`` / ``get_macro_series`` / ``get_regime`` /
``upsert_regime`` are patched on the ``detect`` module. ``upsert_regime``
is routed to the real implementation with a mock session so the label
derivation and rounding in ``regime.store`` are exercised, not faked.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd

from stockscan.regime.detect import MIN_BENCHMARK_BARS, detect_regime
from stockscan.regime.rules import VOL_HIGH_RANK
from stockscan.regime.store import (
    METHODOLOGY_VERSION,
    MarketRegime,
    regime_label,
)
from stockscan.regime.store import upsert_regime as _real_upsert

AS_OF = date(2026, 4, 28)


def _spy_bars(n: int, *, trend: str = "up", burst: bool = False) -> pd.DataFrame:
    """Synthetic SPY bars ending the business day before ``AS_OF``."""
    idx = pd.date_range(end="2026-04-27 21:00", periods=n, freq="B", tz="UTC")
    if trend == "up":
        close = np.linspace(350.0, 520.0, n)
    else:
        close = np.linspace(520.0, 350.0, n)
    if burst:
        # ±3% alternating closes over the last 30 bars: top-tercile vol.
        tail = np.array([1.03 if i % 2 else 1.0 for i in range(30)]) * close[-30]
        close = np.concatenate([close[:-30], tail])
    return pd.DataFrame(
        {"open": close, "high": close * 1.01, "low": close * 0.99, "close": close},
        index=idx,
    )


def _oas(n: int, *, stress: bool = False) -> pd.Series:
    idx = pd.date_range(end="2026-04-27", periods=n, freq="B")
    values = np.full(n, 3.5)
    if stress:
        values[-10:] = np.linspace(4.0, 6.5, 10)
    return pd.Series(values, index=idx)


def _regime(**overrides) -> MarketRegime:
    base = dict(
        as_of_date=AS_OF,
        regime="risk_on",
        trend_gate_open=True,
        days_on_side=40,
        spy_close=Decimal("520.0"),
        spy_sma200=Decimal("480.0"),
        spy_sma200_slope_20d=Decimal("0.01"),
        realized_vol_20d=Decimal("0.12"),
        realized_vol_pct_rank=Decimal("0.3"),
        vol_scalar=Decimal("1.0"),
        hy_oas_level=Decimal("3.5"),
        hy_oas_pct_rank=Decimal("0.4"),
        credit_stress_flag=False,
    )
    base.update(overrides)
    return MarketRegime(**base)


def _upsert_with_mock_session(*args, **kwargs):
    kwargs["session"] = MagicMock()
    return _real_upsert(*args, **kwargs)


def _patched(bars, oas=None, cached=None):
    return (
        patch("stockscan.regime.detect.get_regime", return_value=cached),
        patch("stockscan.regime.detect.get_bars", return_value=bars),
        patch("stockscan.regime.detect.get_macro_series", return_value=oas),
        patch("stockscan.regime.detect.upsert_regime", side_effect=_upsert_with_mock_session),
    )


# -----------------------------------------------------------------------
# MarketRegime / regime_label
# -----------------------------------------------------------------------


class TestMarketRegime:
    def test_block_new_longs_when_gate_closed(self):
        assert _regime(trend_gate_open=False).block_new_longs is True

    def test_block_new_longs_when_credit_stress(self):
        assert _regime(trend_gate_open=True, credit_stress_flag=True).block_new_longs is True

    def test_no_block_when_gate_open_and_no_stress(self):
        assert _regime().block_new_longs is False

    def test_vol_multiplier_from_scalar(self):
        assert _regime(vol_scalar=Decimal("0.62")).vol_multiplier == 0.62

    def test_vol_multiplier_neutral_when_missing(self):
        assert _regime(vol_scalar=None).vol_multiplier == 1.0

    def test_default_methodology_version(self):
        assert _regime().methodology_version == METHODOLOGY_VERSION


class TestRegimeLabel:
    def test_risk_on(self):
        assert regime_label(trend_gate_open=True, credit_stress_flag=False) == "risk_on"

    def test_risk_off(self):
        assert regime_label(trend_gate_open=False, credit_stress_flag=False) == "risk_off"

    def test_credit_stress_dominates(self):
        assert regime_label(trend_gate_open=True, credit_stress_flag=True) == "credit_stress"
        assert regime_label(trend_gate_open=False, credit_stress_flag=True) == "credit_stress"


# -----------------------------------------------------------------------
# detect_regime — cache behaviour
# -----------------------------------------------------------------------


class TestDetectRegimeCache:
    def test_returns_cached_row_without_fetching_bars(self):
        cached = _regime()
        with (
            patch("stockscan.regime.detect.get_regime", return_value=cached) as mock_get,
            patch("stockscan.regime.detect.get_bars") as mock_bars,
            patch("stockscan.regime.detect.upsert_regime") as mock_upsert,
        ):
            assert detect_regime(AS_OF) is cached
        mock_get.assert_called_once()
        mock_bars.assert_not_called()
        mock_upsert.assert_not_called()

    def test_stale_methodology_version_is_recomputed(self):
        stale = _regime(methodology_version=METHODOLOGY_VERSION - 1)
        patches = _patched(_spy_bars(300), cached=stale)
        with patches[0], patches[1] as mock_bars, patches[2], patches[3] as mock_upsert:
            result = detect_regime(AS_OF)
        assert result is not stale
        assert result is not None
        assert result.methodology_version == METHODOLOGY_VERSION
        mock_bars.assert_called_once()
        mock_upsert.assert_called_once()

    def test_force_recompute_bypasses_cache(self):
        cached = _regime()
        patches = _patched(_spy_bars(300), cached=cached)
        with patches[0] as mock_get, patches[1], patches[2], patches[3] as mock_upsert:
            result = detect_regime(AS_OF, force_recompute=True)
        assert result is not cached
        mock_get.assert_not_called()
        mock_upsert.assert_called_once()

    def test_session_is_passed_through(self):
        session = MagicMock()
        patches = _patched(_spy_bars(300))
        with patches[0] as mock_get, patches[1] as mock_bars, patches[2], patches[3] as mock_upsert:
            detect_regime(AS_OF, session=session)
        assert mock_get.call_args.kwargs["session"] is session
        assert mock_bars.call_args.kwargs["session"] is session
        assert mock_upsert.call_args.kwargs["session"] is session


# -----------------------------------------------------------------------
# detect_regime — missing / short benchmark data
# -----------------------------------------------------------------------


class TestDetectRegimeMissingData:
    def test_none_when_no_spy_bars(self):
        patches = _patched(pd.DataFrame())
        with patches[0], patches[1], patches[2], patches[3] as mock_upsert:
            assert detect_regime(AS_OF) is None
        mock_upsert.assert_not_called()

    def test_none_when_bars_fetch_raises(self):
        with (
            patch("stockscan.regime.detect.get_regime", return_value=None),
            patch("stockscan.regime.detect.get_bars", side_effect=RuntimeError("no table")),
        ):
            assert detect_regime(AS_OF) is None

    def test_none_when_spy_history_short(self):
        patches = _patched(_spy_bars(MIN_BENCHMARK_BARS - 1))
        with patches[0], patches[1], patches[2], patches[3] as mock_upsert:
            assert detect_regime(AS_OF) is None
        mock_upsert.assert_not_called()

    def test_bars_after_as_of_are_ignored(self):
        # 300 bars ending well after as_of: only the ones on/before count.
        idx = pd.date_range(start="2026-01-01 21:00", periods=300, freq="B", tz="UTC")
        close = np.linspace(400.0, 500.0, 300)
        bars = pd.DataFrame({"close": close}, index=idx)
        patches = _patched(bars)
        with patches[0], patches[1], patches[2], patches[3]:
            assert detect_regime(AS_OF) is None  # < MIN_BENCHMARK_BARS remain

    def test_macro_series_failure_disables_credit_flag_only(self):
        with (
            patch("stockscan.regime.detect.get_regime", return_value=None),
            patch("stockscan.regime.detect.get_bars", return_value=_spy_bars(300)),
            patch("stockscan.regime.detect.get_macro_series", side_effect=RuntimeError("fred down")),
            patch("stockscan.regime.detect.upsert_regime", side_effect=_upsert_with_mock_session),
        ):
            result = detect_regime(AS_OF)
        assert result is not None
        assert result.credit_stress_flag is False
        assert result.hy_oas_level is None
        assert result.regime == "risk_on"

    def test_short_oas_series_disables_credit_flag(self):
        patches = _patched(_spy_bars(300), oas=_oas(100, stress=True))
        with patches[0], patches[1], patches[2], patches[3]:
            result = detect_regime(AS_OF)
        assert result is not None
        assert result.credit_stress_flag is False
        assert result.hy_oas_level is None


# -----------------------------------------------------------------------
# detect_regime — label derivation and stored fields
# -----------------------------------------------------------------------


class TestDetectRegimeLabels:
    def test_uptrend_is_risk_on(self):
        patches = _patched(_spy_bars(300), oas=_oas(300))
        with patches[0], patches[1], patches[2], patches[3]:
            result = detect_regime(AS_OF)
        assert result is not None
        assert result.regime == "risk_on"
        assert result.trend_gate_open is True
        assert result.days_on_side > 0
        assert result.block_new_longs is False
        assert result.as_of_date == AS_OF
        assert result.spy_close == Decimal("520.0")
        assert result.spy_sma200 < result.spy_close
        assert result.hy_oas_level == Decimal("3.5")
        assert result.vol_scalar is not None
        assert result.methodology_version == METHODOLOGY_VERSION

    def test_downtrend_is_risk_off(self):
        patches = _patched(_spy_bars(300, trend="down"))
        with patches[0], patches[1], patches[2], patches[3]:
            result = detect_regime(AS_OF)
        assert result is not None
        assert result.regime == "risk_off"
        assert result.trend_gate_open is False
        assert result.block_new_longs is True

    def test_credit_stress_label_and_block(self):
        patches = _patched(_spy_bars(300), oas=_oas(300, stress=True))
        with patches[0], patches[1], patches[2], patches[3]:
            result = detect_regime(AS_OF)
        assert result is not None
        assert result.regime == "credit_stress"
        assert result.credit_stress_flag is True
        assert result.trend_gate_open is True
        assert result.block_new_longs is True
        assert result.hy_oas_pct_rank == Decimal("1.0")

    def test_vol_burst_lowers_scalar(self):
        patches = _patched(_spy_bars(300, burst=True))
        with patches[0], patches[1], patches[2], patches[3]:
            result = detect_regime(AS_OF)
        assert result is not None
        assert result.realized_vol_pct_rank is not None
        assert float(result.realized_vol_pct_rank) >= VOL_HIGH_RANK
        assert result.vol_scalar == Decimal("0.5")
        assert result.vol_multiplier == 0.5

    def test_calm_market_has_neutral_scalar(self):
        patches = _patched(_spy_bars(300))
        with patches[0], patches[1], patches[2], patches[3]:
            result = detect_regime(AS_OF)
        assert result is not None
        assert result.vol_multiplier == 1.0
