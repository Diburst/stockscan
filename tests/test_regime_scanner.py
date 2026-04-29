"""Tests for the v2 regime soft-sizing integration in ``ScanRunner``.

Replaces the v1 hard-gate tests. The contract being verified:

  * ``_resolve_regime_factor`` returns a ``RegimeFactor`` with the
    multiplier = affinity * composite_mult * stress_mult.
  * Soft-fail to neutral (multiplier=1.0) when regime data is unavailable
    or detection raises.
  * ``block_new_longs`` is True iff ``credit_stress_flag`` is True.
  * Strategies migrated from ``applicable_regimes`` get the right
    derived multipliers via the back-compat shim.

Uses ``MagicMock`` sessions and ``patch`` to stub ``detect_regime`` so
no live database is required.
"""

from __future__ import annotations

import warnings
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

from stockscan.regime.store import MarketRegime
from stockscan.scan.runner import RegimeFactor, ScanRunner
from stockscan.strategies.base import Strategy, StrategyParams

# -----------------------------------------------------------------------
# Throwaway test strategies — defined inside ``warnings.catch_warnings``
# so the v1→v2 shim's DeprecationWarning doesn't pollute -W error runs.
# -----------------------------------------------------------------------
with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)

    class _NoRegimeStrategy(Strategy):
        """No regime preferences — empty mapping, defaults to neutral."""

        name = "_test_no_regime"
        version = "0.0.1"
        display_name = "Test No Regime"
        params_model = StrategyParams

        def required_history(self) -> int:
            return 10

        def signals(self, bars, as_of):  # type: ignore[no-untyped-def]
            return []

        def exit_rules(self, position, bars, as_of):  # type: ignore[no-untyped-def]
            return None

    class _TrendOnlyStrategy(Strategy):
        """Legacy declaration: only runs in trending_up. The shim derives
        ``regime_affinity`` = {trending_up:1, others:0}."""

        name = "_test_trend_only"
        version = "0.0.1"
        display_name = "Test Trend Only"
        params_model = StrategyParams
        applicable_regimes: frozenset[str] = frozenset({"trending_up"})

        def required_history(self) -> int:
            return 10

        def signals(self, bars, as_of):  # type: ignore[no-untyped-def]
            return []

        def exit_rules(self, position, bars, as_of):  # type: ignore[no-untyped-def]
            return None


# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------
def _runner() -> ScanRunner:
    """Bypass ScanRunner.__init__ — we don't need a real session here."""
    r = ScanRunner.__new__(ScanRunner)
    r._owns_session = True
    r._session = None
    return r


def _regime(
    label: str,
    *,
    composite: float | None = None,
    stress: bool = False,
    methodology_version: int = 2,
) -> MarketRegime:
    return MarketRegime(
        as_of_date=date(2026, 4, 28),
        regime=label,  # type: ignore[arg-type]
        adx=Decimal("28.0"),
        spy_close=Decimal("520.0"),
        spy_sma200=Decimal("480.0"),
        composite_score=Decimal(str(composite)) if composite is not None else None,
        credit_stress_flag=stress,
        methodology_version=methodology_version,
    )


# -----------------------------------------------------------------------
# Soft-fail to neutral
# -----------------------------------------------------------------------
class TestRegimeFactorSoftFail:
    def test_no_regime_row_yields_neutral_factor(self):
        runner = _runner()
        with patch("stockscan.scan.runner.detect_regime", return_value=None):
            f = runner._resolve_regime_factor(MagicMock(), _NoRegimeStrategy, date(2026, 4, 28))
        assert f.multiplier == 1.0
        assert f.label is None
        assert f.composite_score is None
        assert f.credit_stress_flag is False
        assert f.block_new_longs is False

    def test_detection_exception_yields_neutral_factor(self):
        runner = _runner()
        with patch(
            "stockscan.scan.runner.detect_regime",
            side_effect=RuntimeError("db unreachable"),
        ):
            f = runner._resolve_regime_factor(MagicMock(), _NoRegimeStrategy, date(2026, 4, 28))
        assert f.multiplier == 1.0
        assert f.label is None
        assert f.block_new_longs is False


# -----------------------------------------------------------------------
# Multiplier math
# -----------------------------------------------------------------------
class TestRegimeFactorMath:
    def test_no_preferences_full_size_in_any_regime(self):
        """Empty regime_affinity -> default_affinity 1.0 -> full size,
        scaled only by composite_mult."""
        runner = _runner()
        regime = _regime("choppy", composite=0.6)
        with patch("stockscan.scan.runner.detect_regime", return_value=regime):
            f = runner._resolve_regime_factor(MagicMock(), _NoRegimeStrategy, date(2026, 4, 28))
        # 1.0 * (0.5 + 0.5*0.6) * 1.0 = 0.8
        assert f.multiplier == 0.8
        assert f.label == "choppy"
        assert f.composite_score == 0.6
        assert f.credit_stress_flag is False
        assert f.block_new_longs is False

    def test_legacy_strategy_in_trending_up_full_size(self):
        runner = _runner()
        regime = _regime("trending_up", composite=1.0)
        with patch("stockscan.scan.runner.detect_regime", return_value=regime):
            f = runner._resolve_regime_factor(MagicMock(), _TrendOnlyStrategy, date(2026, 4, 28))
        # affinity 1.0 * (0.5 + 0.5*1.0) * 1.0 = 1.0
        assert f.multiplier == 1.0

    def test_legacy_strategy_in_excluded_regime_zero_multiplier(self):
        """Shim translates ``applicable_regimes={"trending_up"}`` to
        affinity 0.0 for choppy/trending_down/transitioning. Multiplier
        is 0.0, the runner will reject signals as "regime_zero_size"."""
        runner = _runner()
        regime = _regime("choppy", composite=0.6)
        with patch("stockscan.scan.runner.detect_regime", return_value=regime):
            f = runner._resolve_regime_factor(MagicMock(), _TrendOnlyStrategy, date(2026, 4, 28))
        # affinity 0.0 * anything = 0.0
        assert f.multiplier == 0.0

    def test_missing_composite_uses_neutral_composite_multiplier(self):
        """A v1 cached row would have composite=None — composite_mult
        falls back to 1.0 so the multiplier is just affinity."""
        runner = _runner()
        regime = _regime("trending_up", composite=None, methodology_version=1)
        with patch("stockscan.scan.runner.detect_regime", return_value=regime):
            f = runner._resolve_regime_factor(MagicMock(), _TrendOnlyStrategy, date(2026, 4, 28))
        # affinity 1.0 * 1.0 (neutral) * 1.0 = 1.0
        assert f.multiplier == 1.0
        assert f.composite_score is None


# -----------------------------------------------------------------------
# Credit-stress override
# -----------------------------------------------------------------------
class TestCreditStressOverride:
    def test_stress_halves_multiplier_and_sets_block_new_longs(self):
        runner = _runner()
        regime = _regime("trending_up", composite=1.0, stress=True)
        with patch("stockscan.scan.runner.detect_regime", return_value=regime):
            f = runner._resolve_regime_factor(MagicMock(), _TrendOnlyStrategy, date(2026, 4, 28))
        # 1.0 * (0.5 + 0.5*1.0) * 0.5 = 0.5
        assert f.multiplier == 0.5
        assert f.credit_stress_flag is True
        assert f.block_new_longs is True

    def test_no_stress_block_new_longs_false(self):
        runner = _runner()
        regime = _regime("trending_up", composite=1.0, stress=False)
        with patch("stockscan.scan.runner.detect_regime", return_value=regime):
            f = runner._resolve_regime_factor(MagicMock(), _TrendOnlyStrategy, date(2026, 4, 28))
        assert f.block_new_longs is False


# -----------------------------------------------------------------------
# RegimeFactor dataclass shape
# -----------------------------------------------------------------------
class TestRegimeFactorDataclass:
    def test_default_construction(self):
        f = RegimeFactor(
            multiplier=1.0,
            label=None,
            composite_score=None,
            credit_stress_flag=False,
            block_new_longs=False,
        )
        assert f.multiplier == 1.0
        assert f.label is None
        assert f.block_new_longs is False
