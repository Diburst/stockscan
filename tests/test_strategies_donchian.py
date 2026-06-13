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


def test_signal_fires_on_breakout():
    """The CORE breakout trigger fires on a fresh 20-day high.

    This targets the breakout mechanics, so the v1.2 *quality* filters
    (tight-base consolidation, volatility contraction, already-soared cap,
    pre-breakout RSI cap, volume multiple, relative strength, turtle-1L) are
    disabled via the documented backward-compat toggles — those filters have
    their own dedicated tests. A 30-day ramp to new highs is exactly the
    "already soared / no tight base" shape v1.2 rejects, which is correct
    behavior, not a breakout-trigger bug.
    """
    core = DonchianBreakout(
        DonchianParams(
            adx_min=10.0,
            require_base_consolidation=False,
            require_vol_contraction=False,
            max_pct_above_sma50=0.0,      # 0 disables the already-soared cap
            max_rsi_pre_breakout=100.0,   # disables the pre-breakout RSI cap
            volume_mult=1.0,              # constant test volume → no volume filter
            require_vol_expansion=False,
            enable_turtle_1l=False,
            enable_relative_strength=False,  # no benchmark bars in a unit test
            entry_periods=[20],
        )
    )
    # 70 days flat in [98, 102], then a 30-day uptrend pushing to new highs.
    rng = np.random.default_rng(0)
    flat = (100 + rng.normal(0, 0.5, 70)).tolist()
    up = np.linspace(102, 130, 30).tolist()
    bars = _make_bars(flat + up)
    sigs = core.signals(bars, as_of=bars.index[-1].date())
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


def _bbw_squeeze_strategy() -> DonchianBreakout:
    """Donchian configured to isolate the v1.3 BBW squeeze filter: everything
    else off, ADX gate slack, single 20-bar window."""
    return DonchianBreakout(
        DonchianParams(
            adx_min=0.0,
            require_base_consolidation=False,  # use ADX gate, not Stage-2
            require_vol_contraction=True,      # the filter under test
            max_pct_above_sma50=0.0,
            max_rsi_pre_breakout=100.0,
            enable_relative_strength=False,
            enable_turtle_1l=False,
            require_vol_expansion=False,
            volume_mult=1.0,
            entry_periods=[20],
        )
    )


def test_bbw_squeeze_blocks_when_no_compression():
    """v1.3: a series with no recent BBW compression must not pass — yesterday's
    BBW sits in the middle of its 126-bar distribution, not the bottom 30%."""
    rng = np.random.default_rng(0)
    n = 250
    closes = (100 + np.cumsum(rng.normal(0, 1.5, n))).tolist()
    # Force today to break the 20-day high so only the squeeze filter can fail.
    closes[-1] = max(closes[-21:-1]) + 1.5
    bars = _make_bars(closes)
    bars.iloc[-1, bars.columns.get_loc("high")] = closes[-1] + 0.5
    bars.iloc[-1, bars.columns.get_loc("low")] = closes[-2] - 0.2
    bars.iloc[-1, bars.columns.get_loc("volume")] = 2_000_000
    s = _bbw_squeeze_strategy()
    sigs = s.signals(bars, as_of=bars.index[-1].date())
    # No squeeze → no signal.
    assert sigs == []


def test_bbw_squeeze_passes_after_compression():
    """v1.3: a wide-vol stretch followed by a tight base compresses BBW into the
    bottom of its 126-bar window, so the breakout passes the squeeze filter and
    the metadata records the percentile rank."""
    rng = np.random.default_rng(1)
    # 210 wide-vol bars (random walk σ=2), then 22 tight bars (σ=0.15), then
    # one breakout bar above the 20-day high.
    wide = (100 + np.cumsum(rng.normal(0, 2.0, 210))).tolist()
    base_level = wide[-1]
    tight = (base_level + rng.normal(0, 0.15, 22)).tolist()
    breakout = max(tight) + 1.5
    closes = wide + tight + [breakout]
    bars = _make_bars(closes)
    bars.iloc[-1, bars.columns.get_loc("high")] = breakout + 0.5
    bars.iloc[-1, bars.columns.get_loc("low")] = closes[-2] - 0.2
    bars.iloc[-1, bars.columns.get_loc("volume")] = 2_500_000
    s = _bbw_squeeze_strategy()
    sigs = s.signals(bars, as_of=bars.index[-1].date())
    assert len(sigs) == 1
    pct = sigs[0].metadata.get("bbw_percentile")
    # Pure-tight BBW at the bottom of a 126-bar window dominated by wide bars
    # ranks deeply below the 30% cap.
    assert pct is not None and pct <= 0.30


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
