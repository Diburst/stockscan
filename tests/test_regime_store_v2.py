"""Unit tests for the v2 extensions to the market_regime persistence layer.

Pure tests (no DB) — mock the SQLAlchemy ``Session`` and verify:
  * ``MarketRegime`` defaults match the methodology_version=1 (legacy) shape.
  * ``upsert_regime`` is back-compat: legacy 5-arg call stamps version=1.
  * ``upsert_regime`` accepts v2 kwargs and stamps version=2.
  * ``_row`` correctly maps v2 columns including NULLs.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

from stockscan.regime.store import (
    MarketRegime,
    _row,
    get_regime,
    upsert_regime,
)


# -----------------------------------------------------------------------
# Dataclass defaults (legacy-row shape)
# -----------------------------------------------------------------------
class TestMarketRegimeDefaults:
    def test_legacy_construction_leaves_v2_fields_none(self):
        r = MarketRegime(
            as_of_date=date(2026, 4, 27),
            regime="trending_up",
            adx=Decimal("28.50"),
            spy_close=Decimal("450.00"),
            spy_sma200=Decimal("440.00"),
        )
        # v2 component scores all None.
        assert r.composite_score is None
        assert r.vol_score is None
        assert r.trend_score is None
        assert r.breadth_score is None
        assert r.credit_score is None
        # v2 levels all None.
        assert r.vix_level is None
        assert r.vix_pct_rank is None
        assert r.hy_oas_level is None
        assert r.hy_oas_pct_rank is None
        assert r.hy_oas_zscore is None
        # Always-present defaults.
        assert r.credit_stress_flag is False
        assert r.methodology_version == 1


# -----------------------------------------------------------------------
# upsert_regime — back-compat for v1 callers
# -----------------------------------------------------------------------
class TestUpsertRegimeV1Compat:
    def test_legacy_call_stamps_methodology_version_1(self):
        sess = MagicMock()
        out = upsert_regime(
            date(2026, 4, 27),
            "trending_up",
            adx=28.5,
            spy_close=450.0,
            spy_sma200=440.0,
            session=sess,
        )
        # The bind dict that hit the DB should carry version=1 + NULLs.
        _stmt, params = sess.execute.call_args[0]
        assert params["methver"] == 1
        assert params["composite"] is None
        assert params["vol"] is None
        assert params["stress"] is False
        # Returned dataclass mirrors the bind dict.
        assert out.methodology_version == 1
        assert out.composite_score is None
        assert out.credit_stress_flag is False


# -----------------------------------------------------------------------
# upsert_regime — v2 path
# -----------------------------------------------------------------------
class TestUpsertRegimeV2:
    def test_v2_call_passes_all_components_and_version_2(self):
        sess = MagicMock()
        out = upsert_regime(
            date(2026, 4, 27),
            "trending_up",
            adx=28.5,
            spy_close=450.0,
            spy_sma200=440.0,
            composite_score=0.72,
            vol_score=0.85,
            trend_score=0.60,
            breadth_score=0.65,
            credit_score=0.78,
            vix_level=14.2,
            vix_pct_rank=0.20,
            hy_oas_level=3.45,
            hy_oas_pct_rank=0.30,
            hy_oas_zscore=-0.5,
            credit_stress_flag=False,
            methodology_version=2,
            session=sess,
        )
        _stmt, params = sess.execute.call_args[0]
        assert params["methver"] == 2
        assert params["composite"] == Decimal("0.72")
        assert params["vol"] == Decimal("0.85")
        assert params["trend"] == Decimal("0.6")
        assert params["breadth"] == Decimal("0.65")
        assert params["credit"] == Decimal("0.78")
        assert params["vix"] == Decimal("14.2")
        assert params["vix_rank"] == Decimal("0.2")
        assert params["hy_oas"] == Decimal("3.45")
        assert params["hy_rank"] == Decimal("0.3")
        assert params["hy_z"] == Decimal("-0.5")
        # Returned dataclass should match what we sent.
        assert out.methodology_version == 2
        assert out.composite_score == Decimal("0.72")

    def test_credit_stress_flag_true(self):
        sess = MagicMock()
        out = upsert_regime(
            date(2026, 4, 27),
            "trending_down",
            adx=32.0,
            spy_close=400.0,
            spy_sma200=420.0,
            composite_score=0.25,
            credit_score=0.10,
            credit_stress_flag=True,
            methodology_version=2,
            session=sess,
        )
        _stmt, params = sess.execute.call_args[0]
        assert params["stress"] is True
        assert out.credit_stress_flag is True

    def test_partial_v2_fields_are_okay(self):
        """If only some components computed (FRED down, breadth missing),
        the upsert still goes through with NULLs for the rest."""
        sess = MagicMock()
        upsert_regime(
            date(2026, 4, 27),
            "trending_up",
            adx=28.5,
            spy_close=450.0,
            spy_sma200=440.0,
            vol_score=0.85,  # only vol available
            vix_level=14.2,
            methodology_version=2,
            session=sess,
        )
        _stmt, params = sess.execute.call_args[0]
        assert params["vol"] == Decimal("0.85")
        assert params["credit"] is None
        assert params["breadth"] is None
        assert params["composite"] is None  # composite not computed -> NULL


# -----------------------------------------------------------------------
# _row — handle NULLs in DB result rows
# -----------------------------------------------------------------------
class TestRowMapping:
    def test_legacy_row_with_null_v2_columns(self):
        legacy_row = SimpleNamespace(
            as_of_date=date(2025, 1, 5),
            regime="choppy",
            adx=Decimal("16.00"),
            spy_close=Decimal("440.00"),
            spy_sma200=Decimal("450.00"),
            composite_score=None,
            vol_score=None,
            trend_score=None,
            breadth_score=None,
            credit_score=None,
            vix_level=None,
            vix_pct_rank=None,
            hy_oas_level=None,
            hy_oas_pct_rank=None,
            hy_oas_zscore=None,
            credit_stress_flag=False,
            methodology_version=1,
        )
        m = _row(legacy_row)
        assert m.regime == "choppy"
        assert m.composite_score is None
        assert m.methodology_version == 1
        assert m.credit_stress_flag is False

    def test_v2_row_full(self):
        v2_row = SimpleNamespace(
            as_of_date=date(2026, 4, 27),
            regime="trending_up",
            adx=Decimal("28.50"),
            spy_close=Decimal("450.00"),
            spy_sma200=Decimal("440.00"),
            composite_score=Decimal("0.7200"),
            vol_score=Decimal("0.8500"),
            trend_score=Decimal("0.6000"),
            breadth_score=Decimal("0.6500"),
            credit_score=Decimal("0.7800"),
            vix_level=Decimal("14.2000"),
            vix_pct_rank=Decimal("0.2000"),
            hy_oas_level=Decimal("3.4500"),
            hy_oas_pct_rank=Decimal("0.3000"),
            hy_oas_zscore=Decimal("-0.5000"),
            credit_stress_flag=False,
            methodology_version=2,
        )
        m = _row(v2_row)
        assert m.composite_score == Decimal("0.7200")
        assert m.vix_level == Decimal("14.2000")
        assert m.methodology_version == 2


# -----------------------------------------------------------------------
# get_regime — surfaces v2 fields end-to-end
# -----------------------------------------------------------------------
class TestGetRegime:
    def test_returns_full_v2_dataclass(self):
        sess = MagicMock()
        sess.execute.return_value.first.return_value = SimpleNamespace(
            as_of_date=date(2026, 4, 27),
            regime="trending_up",
            adx=Decimal("28.50"),
            spy_close=Decimal("450.00"),
            spy_sma200=Decimal("440.00"),
            composite_score=Decimal("0.7200"),
            vol_score=Decimal("0.8500"),
            trend_score=Decimal("0.6000"),
            breadth_score=Decimal("0.6500"),
            credit_score=Decimal("0.7800"),
            vix_level=Decimal("14.2000"),
            vix_pct_rank=Decimal("0.2000"),
            hy_oas_level=Decimal("3.4500"),
            hy_oas_pct_rank=Decimal("0.3000"),
            hy_oas_zscore=Decimal("-0.5000"),
            credit_stress_flag=False,
            methodology_version=2,
        )
        m = get_regime(date(2026, 4, 27), session=sess)
        assert m is not None
        assert m.composite_score == Decimal("0.7200")
        assert m.methodology_version == 2

    def test_returns_none_when_not_found(self):
        sess = MagicMock()
        sess.execute.return_value.first.return_value = None
        assert get_regime(date(2026, 4, 27), session=sess) is None
