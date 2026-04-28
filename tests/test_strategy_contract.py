"""Strategy contract tests (DESIGN §4.11).

Any concrete Strategy subclass must satisfy the invariants below. The
parameterized test runs against every registered strategy.

In Phase 0 we ship the framework + a tiny test strategy to prove the
contract works end-to-end. Real strategies (RSI(2), Donchian) land in
Phase 1.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
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
    discover_strategies,
)


# --------------------------------------------------------------------
# A minimal in-test strategy used to verify the contract machinery.
# --------------------------------------------------------------------
class _NoopParams(StrategyParams):
    threshold: float = Field(0.0, ge=0)


class _NoopStrategy(Strategy):
    name = "noop_test"
    version = "0.0.1"
    display_name = "Noop (test)"
    description = "Test strategy that emits no signals."
    tags = ("test",)
    params_model = _NoopParams

    def required_history(self) -> int:
        return 1

    def signals(self, bars: pd.DataFrame, as_of: date) -> list[RawSignal]:
        return []

    def exit_rules(
        self, position: PositionSnapshot, bars: pd.DataFrame, as_of: date
    ) -> ExitDecision | None:
        return None


# --------------------------------------------------------------------
# Registry-level tests
# --------------------------------------------------------------------
def test_subclassing_auto_registers() -> None:
    assert "noop_test" in STRATEGY_REGISTRY


def test_duplicate_name_raises() -> None:
    with pytest.raises(ValueError, match="collision"):
        # Deliberately reuse the same name.
        class _Dup(Strategy):
            name = "noop_test"
            version = "0.0.2"
            display_name = "Dup"
            params_model = _NoopParams

            def required_history(self) -> int:
                return 1

            def signals(self, bars, as_of):
                return []

            def exit_rules(self, position, bars, as_of):
                return None


def test_missing_required_attr_raises() -> None:
    with pytest.raises(TypeError, match="missing required class attribute"):
        class _Bad(Strategy):
            # missing `name`
            version = "0.0.1"
            display_name = "Bad"
            params_model = _NoopParams

            def required_history(self) -> int:
                return 1

            def signals(self, bars, as_of):
                return []

            def exit_rules(self, position, bars, as_of):
                return None


def test_discover_strategies_returns_count() -> None:
    n = discover_strategies()
    assert n == len(STRATEGY_REGISTRY)


# --------------------------------------------------------------------
# Per-strategy contract — runs against every registered strategy.
# --------------------------------------------------------------------
def _all_strategies() -> list[type[Strategy]]:
    discover_strategies()
    return STRATEGY_REGISTRY.all()


@pytest.fixture
def sample_bars() -> pd.DataFrame:
    """Two months of synthetic AAPL daily bars."""
    idx = pd.date_range("2025-01-01", periods=42, freq="B", tz="UTC")
    return pd.DataFrame(
        {
            "open": [100.0 + i * 0.5 for i in range(42)],
            "high": [101.0 + i * 0.5 for i in range(42)],
            "low": [99.0 + i * 0.5 for i in range(42)],
            "close": [100.5 + i * 0.5 for i in range(42)],
            "adj_close": [100.5 + i * 0.5 for i in range(42)],
            "volume": [1_000_000] * 42,
        },
        index=idx,
    )


@pytest.mark.parametrize("strategy_cls", _all_strategies(), ids=lambda c: c.name)
def test_required_history_is_positive(strategy_cls: type[Strategy]) -> None:
    inst = strategy_cls(strategy_cls.params_model())
    n = inst.required_history()
    assert isinstance(n, int)
    assert 0 < n < 1000


@pytest.mark.parametrize("strategy_cls", _all_strategies(), ids=lambda c: c.name)
def test_signals_idempotent(strategy_cls: type[Strategy], sample_bars: pd.DataFrame) -> None:
    inst = strategy_cls(strategy_cls.params_model())
    a = inst.signals(sample_bars.copy(), as_of=sample_bars.index[-1].date())
    b = inst.signals(sample_bars.copy(), as_of=sample_bars.index[-1].date())
    assert a == b


@pytest.mark.parametrize("strategy_cls", _all_strategies(), ids=lambda c: c.name)
def test_signals_no_lookahead(strategy_cls: type[Strategy], sample_bars: pd.DataFrame) -> None:
    """Slicing future bars off must produce identical output."""
    inst = strategy_cls(strategy_cls.params_model())
    as_of = sample_bars.index[20].date()
    truncated = sample_bars[sample_bars.index.date <= as_of]
    full = sample_bars
    a = inst.signals(truncated, as_of=as_of)
    b = inst.signals(full, as_of=as_of)
    assert a == b


@pytest.mark.parametrize("strategy_cls", _all_strategies(), ids=lambda c: c.name)
def test_exit_rules_returns_none_or_exit(
    strategy_cls: type[Strategy], sample_bars: pd.DataFrame
) -> None:
    inst = strategy_cls(strategy_cls.params_model())
    pos = PositionSnapshot(
        symbol="AAPL",
        qty=10,
        avg_cost=Decimal("100.0"),
        opened_at=datetime(2025, 1, 5, 16, tzinfo=timezone.utc),
        strategy=strategy_cls.name,
    )
    out = inst.exit_rules(pos, sample_bars, as_of=sample_bars.index[-1].date())
    assert out is None or isinstance(out, ExitDecision)


def test_strategy_metadata_helpers() -> None:
    cls = _NoopStrategy
    schema = cls.params_json_schema()
    assert isinstance(schema, dict)
    assert "properties" in schema

    h = cls.hash_params(_NoopParams(threshold=1.0))
    assert isinstance(h, str)
    assert len(h) == 64  # SHA-256 hex


def test_params_validation_rejects_wrong_type() -> None:
    """Strategy must reject params that aren't an instance of params_model."""
    with pytest.raises(TypeError):
        _NoopStrategy(params="not a params object")  # type: ignore[arg-type]
