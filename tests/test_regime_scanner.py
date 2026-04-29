"""Tests for regime-gate integration in ScanRunner.

Uses a minimal fake strategy and stubs out DB/bars access so no live
database is required.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

from stockscan.regime.store import MarketRegime
from stockscan.scan.runner import ScanRunner
from stockscan.strategies.base import Strategy, StrategyParams

# -----------------------------------------------------------------------
# Minimal test strategies
# -----------------------------------------------------------------------

class _NoRegimeStrategy(Strategy):
    """Runs in all regimes (applicable_regimes empty)."""
    name = "_test_no_regime"
    version = "0.0.1"
    display_name = "Test No Regime"
    params_model = StrategyParams
    applicable_regimes: frozenset[str] = frozenset()

    def required_history(self) -> int:
        return 10

    def signals(self, bars, as_of):
        return []

    def exit_rules(self, position, bars, as_of):
        return None


class _TrendOnlyStrategy(Strategy):
    """Only runs in trending_up."""
    name = "_test_trend_only"
    version = "0.0.1"
    display_name = "Test Trend Only"
    params_model = StrategyParams
    applicable_regimes: frozenset[str] = frozenset({"trending_up"})

    def required_history(self) -> int:
        return 10

    def signals(self, bars, as_of):
        return []

    def exit_rules(self, position, bars, as_of):
        return None


# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------

def _make_regime(label: str) -> MarketRegime:
    return MarketRegime(
        as_of_date=date(2026, 4, 28),
        regime=label,  # type: ignore[arg-type]
        adx=Decimal("28.0"),
        spy_close=Decimal("520.0"),
        spy_sma200=Decimal("480.0"),
    )


def _runner_with_stubs(regime: MarketRegime | None):
    """Return a ScanRunner patched so _run_in_session exits after the regime gate."""
    runner = ScanRunner.__new__(ScanRunner)
    runner._owns_session = True
    runner._session = None
    return runner


# -----------------------------------------------------------------------
# Tests
# -----------------------------------------------------------------------

class TestRegimeGate:
    def test_no_applicable_regimes_always_runs(self):
        """Strategy with empty applicable_regimes should never be skipped."""
        runner = ScanRunner.__new__(ScanRunner)
        mock_session = MagicMock()
        regime = _make_regime("choppy")

        with patch("stockscan.scan.runner.detect_regime", return_value=regime):
            result = runner._check_regime(mock_session, _NoRegimeStrategy, date(2026, 4, 28))

        assert result is None  # None = proceed

    def test_matching_regime_returns_none(self):
        """Strategy whose applicable_regimes includes current regime should proceed."""
        runner = ScanRunner.__new__(ScanRunner)
        mock_session = MagicMock()
        regime = _make_regime("trending_up")

        with patch("stockscan.scan.runner.detect_regime", return_value=regime):
            result = runner._check_regime(mock_session, _TrendOnlyStrategy, date(2026, 4, 28))

        assert result is None  # None = proceed

    def test_mismatched_regime_returns_skipped_summary(self):
        """Strategy whose applicable_regimes excludes current regime should be skipped."""
        runner = ScanRunner.__new__(ScanRunner)
        mock_session = MagicMock()
        regime = _make_regime("choppy")  # _TrendOnlyStrategy only runs in trending_up

        with patch("stockscan.scan.runner.detect_regime", return_value=regime):
            result = runner._check_regime(mock_session, _TrendOnlyStrategy, date(2026, 4, 28))

        assert result is not None
        assert result.regime_skipped is True
        assert result.run_id == -1
        assert result.signals_emitted == 0
        assert result.strategy_name == "_test_trend_only"

    def test_unknown_regime_does_not_skip(self):
        """When detect_regime returns None (no SPY data), strategy should still run."""
        runner = ScanRunner.__new__(ScanRunner)
        mock_session = MagicMock()

        with patch("stockscan.scan.runner.detect_regime", return_value=None):
            result = runner._check_regime(mock_session, _TrendOnlyStrategy, date(2026, 4, 28))

        assert result is None  # None = proceed (graceful degradation)

    def test_regime_detection_error_does_not_skip(self):
        """An exception in detect_regime should degrade gracefully, not skip the strategy."""
        runner = ScanRunner.__new__(ScanRunner)
        mock_session = MagicMock()

        with patch("stockscan.scan.runner.detect_regime", side_effect=RuntimeError("db error")):
            result = runner._check_regime(mock_session, _TrendOnlyStrategy, date(2026, 4, 28))

        assert result is None  # proceed despite error

    def test_all_regime_labels_against_trend_only(self):
        """Verify the exact pass/fail for each label against _TrendOnlyStrategy."""
        runner = ScanRunner.__new__(ScanRunner)
        mock_session = MagicMock()

        expected: dict[str, bool] = {
            "trending_up": False,    # False = NOT skipped (runs)
            "trending_down": True,   # True  = skipped
            "choppy": True,
            "transitioning": True,
        }
        for label, should_skip in expected.items():
            regime = _make_regime(label)
            with patch("stockscan.scan.runner.detect_regime", return_value=regime):
                result = runner._check_regime(mock_session, _TrendOnlyStrategy, date(2026, 4, 28))
            if should_skip:
                assert result is not None and result.regime_skipped, f"expected skip for {label}"
            else:
                assert result is None, f"expected no skip for {label}"
