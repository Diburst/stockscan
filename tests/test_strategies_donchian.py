"""Donchian Breakout behavioral tests."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from stockscan.strategies import PositionSnapshot
from stockscan.strategies.donchian_trend import DonchianBreakout, DonchianParams


def _make_bars(closes: list[float], symbol: str = "TEST") -> pd.DataFrame:
    n = len(closes)
    idx = pd.date_range("2024-01-01", periods=n, freq="B", tz="UTC")
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c + 1 for c in closes],
            "low": [c - 1 for c in closes],
            "close": closes,
            "adj_close": closes,
            "volume": [1_000_000] * n,
            "symbol": [symbol] * n,
        },
        index=idx,
    )


@pytest.fixture
def strategy() -> DonchianBreakout:
    # Lower the ADX min in tests so the synthetic linear trends qualify
    # without requiring a fully realistic +DI/−DI buildup.
    return DonchianBreakout(DonchianParams(adx_min=10.0))


def test_no_signal_when_below_recent_high(strategy):
    """Random walk that doesn't make a new 20-day high → no signal."""
    rng = np.random.default_rng(7)
    closes = (100 + np.cumsum(rng.normal(0, 0.2, 100))).tolist()
    bars = _make_bars(closes)
    sigs = strategy.signals(bars, as_of=bars.index[-1].date())
    # Acceptable: zero signals (rarely a new high).
    # Strong assertion: any signal that fires has close > prior 20-day high.
    for s in sigs:
        chan = bars["high"].rolling(20).max().shift(1).iloc[-1]
        assert float(s.suggested_entry) > chan


def test_signal_fires_on_breakout(strategy):
    """Build a trending series that ends at a fresh 20-day high."""
    # 70 days flat in [98, 102], then a 30-day uptrend pushing to new highs.
    rng = np.random.default_rng(0)
    flat = (100 + rng.normal(0, 0.5, 70)).tolist()
    up = np.linspace(102, 130, 30).tolist()
    bars = _make_bars(flat + up)
    sigs = strategy.signals(bars, as_of=bars.index[-1].date())
    assert len(sigs) == 1
    sig = sigs[0]
    assert sig.side == "long"
    assert sig.suggested_stop < sig.suggested_entry


def test_signals_idempotent(strategy):
    flat = [100.0] * 70
    up = np.linspace(102, 130, 30).tolist()
    bars = _make_bars(flat + up)
    a = strategy.signals(bars.copy(), as_of=bars.index[-1].date())
    b = strategy.signals(bars.copy(), as_of=bars.index[-1].date())
    assert a == b


def test_exit_chandelier_stop(strategy):
    """Big rip up, then sharp drop → chandelier trailing stop should fire."""
    rip = np.linspace(100, 200, 80).tolist()
    drop = np.linspace(200, 150, 5).tolist()  # sharp pullback
    bars = _make_bars(rip + drop)
    pos = PositionSnapshot(
        symbol="TEST",
        qty=10,
        avg_cost=Decimal("110"),
        opened_at=datetime(2024, 1, 5, 16, tzinfo=timezone.utc),
        strategy="donchian_trend",
    )
    decision = strategy.exit_rules(pos, bars, as_of=bars.index[-1].date())
    assert decision is not None
    assert decision.reason in {"chandelier_stop", "exit_below_n_day_low"}
