"""Strategy contract tests (DESIGN §4.11).

Any concrete Strategy subclass must satisfy the invariants below. The
parameterized tests run against every registered strategy. The two sector
primitives are patched at their call sites so no DB is touched.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from stockscan.strategies import (
    STRATEGY_REGISTRY,
    ExitDecision,
    PositionSnapshot,
    RawSignal,
    Strategy,
    discover_strategies,
)

_SECTOR_PATCH_TARGETS = (
    "stockscan.strategies.rsi2_meanrev.sector_return",
    "stockscan.strategies.rsi2_meanrev.sector_relative_return",
    "stockscan.strategies.momentum_52w.sector_relative_return",
)


@pytest.fixture(autouse=True)
def _no_db_sector_legs(monkeypatch):
    for target in _SECTOR_PATCH_TARGETS:
        monkeypatch.setattr(target, lambda bars, as_of, *, lookback: 0.0)


# --------------------------------------------------------------------
# A minimal in-test strategy used to verify the contract machinery.
# --------------------------------------------------------------------
class _NoopStrategy(Strategy):
    name = "noop_test"
    version = "0.0.1"
    display_name = "Noop (test)"
    description = "Test strategy that emits no signals."
    tags = ("test",)
    position_pct = 0.05
    threshold = 0.0

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
    assert STRATEGY_REGISTRY.get("noop_test") is _NoopStrategy


def test_duplicate_name_raises() -> None:
    with pytest.raises(ValueError, match="collision"):

        class _Dup(Strategy):
            name = "noop_test"
            version = "0.0.2"
            display_name = "Dup"

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

            def required_history(self) -> int:
                return 1

            def signals(self, bars, as_of):
                return []

            def exit_rules(self, position, bars, as_of):
                return None


def test_abstract_intermediate_is_not_registered() -> None:
    class _Base(Strategy):
        __abstract__ = True

        def required_history(self) -> int:
            return 1

        def signals(self, bars, as_of):
            return []

        def exit_rules(self, position, bars, as_of):
            return None

    assert _Base not in STRATEGY_REGISTRY.all()


def test_discover_strategies_returns_count() -> None:
    n = discover_strategies()
    assert n == len(STRATEGY_REGISTRY)
    assert {"rsi2_meanrev", "momentum_52w_high"} <= set(STRATEGY_REGISTRY.names())


def test_unknown_name_raises_keyerror() -> None:
    with pytest.raises(KeyError, match="Unknown strategy"):
        STRATEGY_REGISTRY.get("does_not_exist")


# --------------------------------------------------------------------
# Per-strategy contract — runs against every registered strategy.
# --------------------------------------------------------------------
def _all_strategies() -> list[type[Strategy]]:
    discover_strategies()
    return STRATEGY_REGISTRY.all()


def _bars(n: int, start: str = "2023-01-02") -> pd.DataFrame:
    """A smooth climb with a two-day dip at the end: long enough for every
    strategy's ``required_history`` and shaped so entry gates can pass."""
    closes = list(100.0 * np.exp(np.linspace(0.0, 0.5, n - 2)))
    closes += [closes[-1] * 0.97, closes[-1] * 0.94]
    idx = pd.date_range(start, periods=n, freq="B", tz="UTC")
    df = pd.DataFrame(
        {
            "open": closes,
            "high": [c * 1.005 for c in closes],
            "low": [c * 0.995 for c in closes],
            "close": closes,
            "adj_close": closes,
            "volume": [1_000_000.0] * n,
        },
        index=idx,
    )
    df.attrs["symbol"] = "AAPL"
    return df


@pytest.fixture
def sample_bars() -> pd.DataFrame:
    return _bars(400)


def _position(strategy_cls: type[Strategy], bars: pd.DataFrame) -> PositionSnapshot:
    return PositionSnapshot(
        symbol="AAPL",
        qty=10,
        avg_cost=Decimal("100.0"),
        opened_at=bars.index[-20].to_pydatetime(),
        strategy=strategy_cls.name,
    )


@pytest.mark.parametrize("strategy_cls", _all_strategies(), ids=lambda c: c.name)
def test_instantiates_without_arguments(strategy_cls: type[Strategy]) -> None:
    inst = strategy_cls()
    assert isinstance(inst, Strategy)
    for attr in ("name", "version", "display_name"):
        assert isinstance(getattr(strategy_cls, attr), str) and getattr(strategy_cls, attr)


@pytest.mark.parametrize("strategy_cls", _all_strategies(), ids=lambda c: c.name)
def test_required_history_is_positive(strategy_cls: type[Strategy]) -> None:
    n = strategy_cls().required_history()
    assert isinstance(n, int)
    assert 0 < n < 1000


@pytest.mark.parametrize("strategy_cls", _all_strategies(), ids=lambda c: c.name)
def test_required_history_covers_longest_lookback(strategy_cls: type[Strategy]) -> None:
    """Every ``*_period`` / ``*_lookback`` / ``*_window`` / ``*_baseline`` knob
    must fit inside the history the strategy asks for."""
    lookbacks = [
        v
        for k, v in strategy_cls.knobs().items()
        if isinstance(v, int) and not isinstance(v, bool)
        and k.endswith(("_period", "_lookback", "_window", "_baseline", "_bars"))
    ]
    if not lookbacks:
        pytest.skip("no lookback knobs")
    assert strategy_cls().required_history() >= max(lookbacks)


@pytest.mark.parametrize("strategy_cls", _all_strategies(), ids=lambda c: c.name)
def test_declares_sizing_basis(strategy_cls: type[Strategy]) -> None:
    """Either a fixed fraction (no stop) or a risk budget for a stop."""
    if strategy_cls.position_pct is not None:
        assert 0 < strategy_cls.position_pct <= 1
    else:
        assert 0 < strategy_cls.default_risk_pct < 0.1


@pytest.mark.parametrize("strategy_cls", _all_strategies(), ids=lambda c: c.name)
def test_signals_shape_and_idempotence(
    strategy_cls: type[Strategy], sample_bars: pd.DataFrame
) -> None:
    inst = strategy_cls()
    as_of = sample_bars.index[-1].date()
    a = inst.signals(sample_bars.copy(), as_of=as_of)
    b = inst.signals(sample_bars.copy(), as_of=as_of)
    assert a == b
    assert isinstance(a, list)
    for sig in a:
        assert isinstance(sig, RawSignal)
        assert sig.strategy_name == strategy_cls.name
        assert sig.strategy_version == strategy_cls.version
        assert sig.symbol == "AAPL"
        assert sig.side in ("long", "short")
        assert isinstance(sig.score, Decimal)
        assert isinstance(sig.suggested_entry, Decimal) and sig.suggested_entry > 0
        if sig.suggested_stop is None:
            assert strategy_cls.position_pct is not None, (
                "a strategy without a stop must size by fixed fraction"
            )
        else:
            assert isinstance(sig.suggested_stop, Decimal)
            assert 0 < sig.suggested_stop < sig.suggested_entry
        assert isinstance(sig.metadata, dict)


@pytest.mark.parametrize("strategy_cls", _all_strategies(), ids=lambda c: c.name)
def test_signals_no_lookahead(strategy_cls: type[Strategy]) -> None:
    """Bars after ``as_of`` must not change the output, on every weekday."""
    inst = strategy_cls()
    bars = _bars(420)
    for cut in range(380, 385):
        as_of = bars.index[cut].date()
        truncated = bars[bars.index.date <= as_of]
        truncated.attrs["symbol"] = "AAPL"
        assert inst.signals(bars, as_of=as_of) == inst.signals(truncated, as_of=as_of)


@pytest.mark.parametrize("strategy_cls", _all_strategies(), ids=lambda c: c.name)
def test_exit_rules_returns_none_or_exit(
    strategy_cls: type[Strategy], sample_bars: pd.DataFrame
) -> None:
    inst = strategy_cls()
    pos = _position(strategy_cls, sample_bars)
    out = inst.exit_rules(pos, sample_bars, as_of=sample_bars.index[-1].date())
    assert out is None or isinstance(out, ExitDecision)
    if out is not None:
        assert out.reason
        assert 0 < out.qty <= pos.qty


@pytest.mark.parametrize("strategy_cls", _all_strategies(), ids=lambda c: c.name)
def test_exit_rules_no_lookahead(strategy_cls: type[Strategy]) -> None:
    inst = strategy_cls()
    bars = _bars(420)
    pos = _position(strategy_cls, bars)
    for cut in range(380, 385):
        as_of = bars.index[cut].date()
        truncated = bars[bars.index.date <= as_of]
        truncated.attrs["symbol"] = "AAPL"
        assert inst.exit_rules(pos, bars, as_of=as_of) == inst.exit_rules(
            pos, truncated, as_of=as_of
        )


# --------------------------------------------------------------------
# Knobs
# --------------------------------------------------------------------
_PRIMITIVES = (int, float, str, bool)


@pytest.mark.parametrize("strategy_cls", _all_strategies(), ids=lambda c: c.name)
def test_knobs_are_primitives_with_sizing_and_without_metadata(
    strategy_cls: type[Strategy],
) -> None:
    knobs = strategy_cls.knobs()
    assert isinstance(knobs, dict) and knobs
    for key, value in knobs.items():
        assert isinstance(key, str) and not key.startswith("_")
        assert isinstance(value, _PRIMITIVES), key
    for sizing in ("default_risk_pct", "sizes_down_in_high_vol"):
        assert sizing in knobs
    assert ("position_pct" in knobs) == (strategy_cls.position_pct is not None)
    assert ("max_open_positions" in knobs) == (strategy_cls.max_open_positions is not None)
    for meta in ("name", "version", "display_name", "description", "manual", "tags",
                 "data_dependencies"):
        assert meta not in knobs


def test_knobs_include_subclass_constants() -> None:
    knobs = _NoopStrategy.knobs()
    assert knobs["threshold"] == 0.0
    assert knobs["position_pct"] == 0.05


@pytest.mark.parametrize("strategy_cls", _all_strategies(), ids=lambda c: c.name)
def test_knobs_hash_is_stable_and_sensitive(strategy_cls: type[Strategy]) -> None:
    h1 = strategy_cls.knobs_hash()
    h2 = strategy_cls.knobs_hash()
    assert h1 == h2
    assert isinstance(h1, str) and len(h1) == 64

    key = next(iter(sorted(strategy_cls.knobs())))
    value = strategy_cls.knobs()[key]
    if isinstance(value, bool):
        new_value = not value
    elif isinstance(value, (int, float)):
        new_value = value + 1
    else:
        new_value = value + "x"

    tweaked = type(
        f"_Tweaked_{strategy_cls.__name__}",
        (strategy_cls,),
        {"name": f"{strategy_cls.name}__tweaked_test", key: new_value},
    )
    assert tweaked.knobs()[key] == new_value
    assert tweaked.knobs_hash() != h1
    # Everything else is inherited unchanged.
    rest = {k: v for k, v in tweaked.knobs().items() if k != key}
    assert rest == {k: v for k, v in strategy_cls.knobs().items() if k != key}


@pytest.mark.parametrize("strategy_cls", _all_strategies(), ids=lambda c: c.name)
def test_code_fingerprint_is_sha256(strategy_cls: type[Strategy]) -> None:
    fp = strategy_cls.code_fingerprint()
    assert len(fp) == 64
    assert fp == strategy_cls.code_fingerprint()


def test_exit_decision_defaults() -> None:
    d = ExitDecision(reason="time_stop", qty=3)
    assert d.order_type == "market_on_open"
    assert d.limit_price is None
    snap = PositionSnapshot(
        symbol="AAPL",
        qty=3,
        avg_cost=Decimal("1"),
        opened_at=datetime(2025, 1, 5, 16, tzinfo=timezone.utc),
        strategy="noop_test",
    )
    assert snap.qty == 3
