"""v2-specific tests for the regime detector.

Covers behavior introduced in migration 0010 / the composite upgrade:
  * Per-component soft-fail (VIX/RSP/HY OAS each missing in isolation).
  * Cache discipline (v1 row gets re-detected; v2 row is reused).
  * ``force_recompute`` bypasses the cache.
  * Composite renormalization when one or more components are missing.

The legacy v1 tests in ``test_regime_detect.py`` still cover the
mandatory-SPY path; this file focuses on the v2 surface.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any
from unittest.mock import patch

import numpy as np
import pandas as pd

from stockscan.regime.detect import detect_regime
from stockscan.regime.store import MarketRegime


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def _spy_bars(n: int = 504, start_date: str = "2024-01-01") -> pd.DataFrame:
    """Synthetic rising SPY bars long enough for both the legacy label
    (≥230) and the v2 trend score (uses SMA(200) + 20-bar slope)."""
    idx = pd.date_range(start_date, periods=n, freq="B")
    close = pd.Series(np.linspace(350.0, 500.0, n), index=idx)
    high = close + 2.0
    low = close - 2.0
    return pd.DataFrame({"open": close, "high": high, "low": low, "close": close})


def _vix_bars(n: int = 504, start_date: str = "2024-01-01") -> pd.DataFrame:
    """Synthetic VIX bars with mild oscillation — gives non-trivial percentile rank."""
    idx = pd.date_range(start_date, periods=n, freq="B")
    rng = np.random.default_rng(42)
    base = 18.0 + 4.0 * np.sin(np.linspace(0.0, 12.0, n))
    noise = rng.normal(0, 1.0, n).cumsum() * 0.1
    close = pd.Series(np.clip(base + noise, 9.0, 60.0), index=idx)
    high = close + 0.5
    low = (close - 0.5).clip(lower=8.0)
    return pd.DataFrame({"open": close, "high": high, "low": low, "close": close})


def _rsp_bars(n: int = 504, start_date: str = "2024-01-01") -> pd.DataFrame:
    """Synthetic RSP bars — slightly outpacing SPY (broadening)."""
    idx = pd.date_range(start_date, periods=n, freq="B")
    close = pd.Series(np.linspace(150.0, 220.0, n), index=idx)  # +47% vs SPY's +43%
    high = close + 1.0
    low = close - 1.0
    return pd.DataFrame({"open": close, "high": high, "low": low, "close": close})


def _hy_oas_series(n: int = 504, start_date: str = "2024-01-01") -> pd.Series:
    """Synthetic HY OAS — slow-moving, stays below stress threshold."""
    idx = pd.date_range(start_date, periods=n, freq="B")
    rng = np.random.default_rng(7)
    levels = 4.0 + np.cumsum(rng.normal(0, 0.05, n))
    s = pd.Series(np.clip(levels, 2.5, 12.0), index=idx)
    s.name = "BAMLH0A0HYM2"
    return s


def _bars_dispatcher(symbol_to_df: dict[str, pd.DataFrame]) -> Any:
    """Build a ``get_bars`` side-effect that returns a different DataFrame per symbol."""

    def _fake(symbol: str, *_args: object, **_kwargs: object) -> pd.DataFrame:
        return symbol_to_df.get(symbol, pd.DataFrame())

    return _fake


def _make_fake_upsert(stored: list[dict[str, Any]]) -> Any:
    """Recording ``upsert_regime`` that captures all kwargs and returns a real MarketRegime."""

    def _fake(as_of, regime, *, adx, spy_close, spy_sma200, **kw):  # type: ignore[no-untyped-def]
        captured = {
            "as_of": as_of,
            "regime": regime,
            "adx": adx,
            "spy_close": spy_close,
            "spy_sma200": spy_sma200,
            **kw,
        }
        stored.append(captured)
        return MarketRegime(
            as_of_date=as_of,
            regime=regime,
            adx=Decimal(str(round(adx, 2))),
            spy_close=Decimal(str(round(spy_close, 4))),
            spy_sma200=Decimal(str(round(spy_sma200, 4))),
            composite_score=(
                Decimal(str(kw["composite_score"]))
                if kw.get("composite_score") is not None
                else None
            ),
            vol_score=(Decimal(str(kw["vol_score"])) if kw.get("vol_score") is not None else None),
            trend_score=(
                Decimal(str(kw["trend_score"])) if kw.get("trend_score") is not None else None
            ),
            breadth_score=(
                Decimal(str(kw["breadth_score"])) if kw.get("breadth_score") is not None else None
            ),
            credit_score=(
                Decimal(str(kw["credit_score"])) if kw.get("credit_score") is not None else None
            ),
            credit_stress_flag=bool(kw.get("credit_stress_flag", False)),
            methodology_version=int(kw.get("methodology_version", 1)),
        )

    return _fake


# ---------------------------------------------------------------------------
# Cache discipline
# ---------------------------------------------------------------------------
class TestCacheDiscipline:
    def test_v1_cached_row_is_re_detected(self):
        """A row stored before migration 0010 should not be returned as-is —
        the v2 path must re-run so the composite columns can backfill."""
        v1_cached = MarketRegime(
            as_of_date=date(2026, 4, 28),
            regime="trending_up",
            adx=Decimal("28.0"),
            spy_close=Decimal("520.0"),
            spy_sma200=Decimal("480.0"),
            methodology_version=1,
        )
        stored: list[dict[str, Any]] = []
        empty_macro = pd.Series(dtype=float, name="x")
        with (
            patch("stockscan.regime.detect.get_regime", return_value=v1_cached),
            patch(
                "stockscan.regime.detect.get_bars",
                side_effect=_bars_dispatcher({"SPY": _spy_bars()}),
            ),
            patch("stockscan.regime.detect.get_macro_series", return_value=empty_macro),
            patch("stockscan.regime.detect.upsert_regime", side_effect=_make_fake_upsert(stored)),
        ):
            result = detect_regime(date(2026, 4, 28))
        assert result is not None
        assert len(stored) == 1, "v1 cached row should trigger one fresh upsert"
        assert stored[0]["methodology_version"] == 2

    def test_v2_cached_row_is_reused(self):
        """A v2 row in the cache must be returned as-is without recomputing."""
        v2_cached = MarketRegime(
            as_of_date=date(2026, 4, 28),
            regime="trending_up",
            adx=Decimal("28.0"),
            spy_close=Decimal("520.0"),
            spy_sma200=Decimal("480.0"),
            composite_score=Decimal("0.65"),
            methodology_version=2,
        )
        with (
            patch("stockscan.regime.detect.get_regime", return_value=v2_cached),
            patch("stockscan.regime.detect.get_bars") as mock_bars,
            patch("stockscan.regime.detect.upsert_regime") as mock_upsert,
        ):
            result = detect_regime(date(2026, 4, 28))
        assert result is v2_cached
        mock_bars.assert_not_called()
        mock_upsert.assert_not_called()

    def test_force_recompute_bypasses_cache(self):
        v2_cached = MarketRegime(
            as_of_date=date(2026, 4, 28),
            regime="trending_up",
            adx=Decimal("28.0"),
            spy_close=Decimal("520.0"),
            spy_sma200=Decimal("480.0"),
            methodology_version=2,
        )
        stored: list[dict[str, Any]] = []
        empty_macro = pd.Series(dtype=float, name="x")
        with (
            patch("stockscan.regime.detect.get_regime", return_value=v2_cached),
            patch(
                "stockscan.regime.detect.get_bars",
                side_effect=_bars_dispatcher({"SPY": _spy_bars()}),
            ),
            patch("stockscan.regime.detect.get_macro_series", return_value=empty_macro),
            patch("stockscan.regime.detect.upsert_regime", side_effect=_make_fake_upsert(stored)),
        ):
            detect_regime(date(2026, 4, 28), force_recompute=True)
        assert len(stored) == 1


# ---------------------------------------------------------------------------
# Per-component soft fail
# ---------------------------------------------------------------------------
class TestComponentSoftFail:
    """Each v2 data source can be missing in isolation; the row still
    persists with NULL for the missing component(s)."""

    def _common_patches(self, stored, *, spy=True, vix=True, rsp=True, hy=True):
        bars_map: dict[str, pd.DataFrame] = {}
        if spy:
            bars_map["SPY"] = _spy_bars()
        if vix:
            bars_map["VIX"] = _vix_bars()
        if rsp:
            bars_map["RSP"] = _rsp_bars()
        macro = _hy_oas_series() if hy else pd.Series(dtype=float, name="x")
        return [
            patch("stockscan.regime.detect.get_regime", return_value=None),
            patch(
                "stockscan.regime.detect.get_bars",
                side_effect=_bars_dispatcher(bars_map),
            ),
            patch("stockscan.regime.detect.get_macro_series", return_value=macro),
            patch("stockscan.regime.detect.upsert_regime", side_effect=_make_fake_upsert(stored)),
        ]

    def test_no_vix_yields_null_vol_score(self):
        stored: list[dict[str, Any]] = []
        patches = self._common_patches(stored, vix=False)
        with patches[0], patches[1], patches[2], patches[3]:
            detect_regime(date(2026, 4, 28))
        assert len(stored) == 1
        row = stored[0]
        assert row["vol_score"] is None
        assert row["vix_level"] is None
        assert row["vix_pct_rank"] is None
        # Other components still populated.
        assert row["trend_score"] is not None
        assert row["breadth_score"] is not None
        assert row["credit_score"] is not None
        # Composite renormalized over the 3 available -> still present.
        assert row["composite_score"] is not None
        assert row["methodology_version"] == 2

    def test_no_rsp_yields_null_breadth(self):
        stored: list[dict[str, Any]] = []
        patches = self._common_patches(stored, rsp=False)
        with patches[0], patches[1], patches[2], patches[3]:
            detect_regime(date(2026, 4, 28))
        row = stored[0]
        assert row["breadth_score"] is None
        assert row["vol_score"] is not None
        assert row["credit_score"] is not None

    def test_no_hy_oas_yields_null_credit(self):
        stored: list[dict[str, Any]] = []
        patches = self._common_patches(stored, hy=False)
        with patches[0], patches[1], patches[2], patches[3]:
            detect_regime(date(2026, 4, 28))
        row = stored[0]
        assert row["credit_score"] is None
        assert row["hy_oas_level"] is None
        assert row["hy_oas_pct_rank"] is None
        assert row["hy_oas_zscore"] is None
        # Stress flag defaults to False when HY OAS unavailable.
        assert row["credit_stress_flag"] is False
        # Other components still populated.
        assert row["vol_score"] is not None
        assert row["breadth_score"] is not None

    def test_only_spy_yields_only_trend_component(self):
        """All three v2 fetches fail. We still get the legacy label and
        a trend-only composite score (vol/breadth/credit all NULL)."""
        stored: list[dict[str, Any]] = []
        patches = self._common_patches(stored, vix=False, rsp=False, hy=False)
        with patches[0], patches[1], patches[2], patches[3]:
            result = detect_regime(date(2026, 4, 28))
        assert result is not None
        row = stored[0]
        assert row["vol_score"] is None
        assert row["breadth_score"] is None
        assert row["credit_score"] is None
        assert row["trend_score"] is not None
        # Composite is just trend_score (renormalized weight = 1.0).
        # That means composite ≈ trend in this case.
        assert row["composite_score"] is not None
        assert abs(float(row["composite_score"]) - float(row["trend_score"])) < 1e-9


# ---------------------------------------------------------------------------
# Credit stress flag end-to-end
# ---------------------------------------------------------------------------
class TestCreditStressFlagEndToEnd:
    def test_stress_flag_fires_with_widening_spreads(self):
        """HY OAS in top 15% AND rising over 5 days -> flag True at as_of."""
        # 504 days of moderate spreads, then a spike at the end.
        n = 504
        idx = pd.date_range("2024-01-01", periods=n, freq="B")
        oas = np.concatenate([np.full(n - 10, 4.0), np.linspace(4.0, 9.0, 10)])
        hy = pd.Series(oas, index=idx, name="BAMLH0A0HYM2")

        stored: list[dict[str, Any]] = []
        with (
            patch("stockscan.regime.detect.get_regime", return_value=None),
            patch(
                "stockscan.regime.detect.get_bars",
                side_effect=_bars_dispatcher({"SPY": _spy_bars()}),
            ),
            patch("stockscan.regime.detect.get_macro_series", return_value=hy),
            patch("stockscan.regime.detect.upsert_regime", side_effect=_make_fake_upsert(stored)),
        ):
            detect_regime(date(2026, 4, 28))
        assert stored[0]["credit_stress_flag"] is True

    def test_stress_flag_does_not_fire_with_steady_spreads(self):
        """Steady ~4% HY OAS -> rank ~0.5, no rising trend -> flag False."""
        stored: list[dict[str, Any]] = []
        with (
            patch("stockscan.regime.detect.get_regime", return_value=None),
            patch(
                "stockscan.regime.detect.get_bars",
                side_effect=_bars_dispatcher({"SPY": _spy_bars()}),
            ),
            patch("stockscan.regime.detect.get_macro_series", return_value=_hy_oas_series()),
            patch("stockscan.regime.detect.upsert_regime", side_effect=_make_fake_upsert(stored)),
        ):
            detect_regime(date(2026, 4, 28))
        assert stored[0]["credit_stress_flag"] is False
