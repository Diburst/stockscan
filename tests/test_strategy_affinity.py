"""Tests for the v2 strategy regime contract: ``regime_affinity`` + the
back-compat shim that derives it from the deprecated ``applicable_regimes``.
"""

from __future__ import annotations

import warnings
from datetime import date
from decimal import Decimal

import pandas as pd
import pytest
from pydantic import Field

from stockscan.strategies import (
    STRATEGY_REGISTRY,
    ExitDecision,
    PositionSnapshot,
    RawSignal,
    Strategy,
    StrategyParams,
)


# --------------------------------------------------------------------------
# Minimal test scaffolding — every test defines its own throwaway strategy
# so the regime-contract behavior is exercised in isolation.
# --------------------------------------------------------------------------
class _Params(StrategyParams):
    threshold: float = Field(0.0, ge=0)


def _scaffolding(name: str) -> dict[str, object]:
    """Common class-attribute payload for a one-off Strategy subclass."""
    return {
        "name": name,
        "version": "0.0.1",
        "display_name": name,
        "params_model": _Params,
    }


def _impls() -> dict[str, object]:
    """Required-method stubs for any in-test Strategy subclass."""
    return {
        "required_history": lambda self: 1,
        "signals": lambda self, bars, as_of: [],
        "exit_rules": lambda self, position, bars, as_of: None,
    }


# --------------------------------------------------------------------------
# Direct regime_affinity declaration
# --------------------------------------------------------------------------
class TestRegimeAffinity:
    def test_explicit_affinity_is_returned_by_lookup(self):
        cls = type(
            "_AffStrat",
            (Strategy,),
            {
                **_scaffolding("test_aff_explicit"),
                **_impls(),
                "regime_affinity": {
                    "trending_up": 1.0,
                    "trending_down": 0.0,
                    "choppy": 0.5,
                    "transitioning": 0.7,
                },
            },
        )
        try:
            assert cls.affinity_for("trending_up") == 1.0
            assert cls.affinity_for("trending_down") == 0.0
            assert cls.affinity_for("choppy") == 0.5
            assert cls.affinity_for("transitioning") == 0.7
        finally:
            STRATEGY_REGISTRY._by_name.pop("test_aff_explicit", None)

    def test_unknown_label_falls_back_to_default_affinity(self):
        cls = type(
            "_AffPartial",
            (Strategy,),
            {
                **_scaffolding("test_aff_partial"),
                **_impls(),
                "regime_affinity": {"trending_up": 1.0},
                "default_affinity": 0.42,
            },
        )
        try:
            assert cls.affinity_for("trending_up") == 1.0
            # Not in the mapping -> default_affinity.
            assert cls.affinity_for("choppy") == 0.42
            assert cls.affinity_for("future_label_we_havent_invented") == 0.42
        finally:
            STRATEGY_REGISTRY._by_name.pop("test_aff_partial", None)

    def test_empty_mapping_means_neutral_default_for_all_labels(self):
        cls = type(
            "_AffEmpty",
            (Strategy,),
            {**_scaffolding("test_aff_empty"), **_impls()},
        )
        try:
            for label in ("trending_up", "trending_down", "choppy", "transitioning"):
                assert cls.affinity_for(label) == 1.0
        finally:
            STRATEGY_REGISTRY._by_name.pop("test_aff_empty", None)


# --------------------------------------------------------------------------
# Back-compat shim: applicable_regimes → regime_affinity
# --------------------------------------------------------------------------
class TestApplicableRegimesShim:
    def test_legacy_set_is_translated_to_affinity_mapping(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            cls = type(
                "_LegacyStrat",
                (Strategy,),
                {
                    **_scaffolding("test_legacy_strat"),
                    **_impls(),
                    "applicable_regimes": frozenset({"trending_up", "choppy"}),
                },
            )
        try:
            # In-set labels get 1.0; out-of-set labels get 0.0.
            assert cls.affinity_for("trending_up") == 1.0
            assert cls.affinity_for("choppy") == 1.0
            assert cls.affinity_for("trending_down") == 0.0
            assert cls.affinity_for("transitioning") == 0.0
            # Deprecation warning surfaced.
            assert any(
                issubclass(w.category, DeprecationWarning)
                and "applicable_regimes" in str(w.message)
                for w in caught
            )
        finally:
            STRATEGY_REGISTRY._by_name.pop("test_legacy_strat", None)

    def test_declaring_both_raises(self):
        with pytest.raises(TypeError, match="declares both"):
            _ = type(
                "_BothStrat",
                (Strategy,),
                {
                    **_scaffolding("test_both_strat"),
                    **_impls(),
                    "applicable_regimes": frozenset({"trending_up"}),
                    "regime_affinity": {"trending_up": 0.5},
                },
            )

    def test_empty_applicable_regimes_does_not_trigger_shim(self):
        """An explicit ``frozenset()`` (i.e., "no preference") is the same
        as not declaring at all — no DeprecationWarning, no derivation."""
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            cls = type(
                "_NoPrefStrat",
                (Strategy,),
                {
                    **_scaffolding("test_no_pref"),
                    **_impls(),
                    "applicable_regimes": frozenset(),
                },
            )
        try:
            assert not any(issubclass(w.category, DeprecationWarning) for w in caught)
            # Empty mapping -> neutral default for all labels.
            assert cls.affinity_for("trending_up") == 1.0
        finally:
            STRATEGY_REGISTRY._by_name.pop("test_no_pref", None)


# --------------------------------------------------------------------------
# Shipped strategies — verify their committed weights
# --------------------------------------------------------------------------
class TestShippedStrategiesAffinity:
    def test_donchian_trend(self):
        from stockscan.strategies.donchian_trend import DonchianTrend

        assert DonchianTrend.affinity_for("trending_up") == 1.0
        assert DonchianTrend.affinity_for("trending_down") == 1.0
        assert DonchianTrend.affinity_for("choppy") == 0.25
        assert DonchianTrend.affinity_for("transitioning") == 0.5

    def test_rsi2_meanrev_zeros_in_trending_down(self):
        from stockscan.strategies.rsi2_meanrev import RSI2MeanReversion

        # The "don't trade in confirmed downtrend" intent must survive
        # the migration intact.
        assert RSI2MeanReversion.affinity_for("trending_down") == 0.0
        assert RSI2MeanReversion.affinity_for("trending_up") == 1.0
        assert RSI2MeanReversion.affinity_for("choppy") == 1.0

    def test_largecap_rebound_zeros_in_trending_down(self):
        from stockscan.strategies.largecap_rebound import LargeCapRebound

        assert LargeCapRebound.affinity_for("trending_down") == 0.0
        assert LargeCapRebound.affinity_for("trending_up") == 1.0


# --------------------------------------------------------------------------
# Sanity: the affinity surface doesn't break instantiation or signals
# --------------------------------------------------------------------------
class TestAffinityDoesNotBreakStrategies:
    def test_subclass_with_affinity_can_be_instantiated_and_run(self):
        cls = type(
            "_LiveStrat",
            (Strategy,),
            {
                **_scaffolding("test_live_strat"),
                **_impls(),
                "regime_affinity": {"trending_up": 0.8},
            },
        )
        try:
            inst = cls(_Params())
            # signals/exit_rules contract still works.
            assert inst.signals(pd.DataFrame(), date(2026, 4, 28)) == []
            pos = PositionSnapshot(
                symbol="AAPL",
                qty=10,
                avg_cost=Decimal("100"),
                opened_at=__import__("datetime").datetime(2026, 4, 1),
                strategy="test_live_strat",
            )
            assert inst.exit_rules(pos, pd.DataFrame(), date(2026, 4, 28)) in (None,)
        finally:
            STRATEGY_REGISTRY._by_name.pop("test_live_strat", None)

    def test_affinity_for_returns_a_float(self):
        from stockscan.strategies.donchian_trend import DonchianTrend

        v = DonchianTrend.affinity_for("trending_up")
        assert isinstance(v, float)


# Suppress unused-import lints for symbols that exist purely to make the
# test module import-resolve cleanly when running under -W error.
_ = (RawSignal, ExitDecision)
