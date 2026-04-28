"""RSI(2) Mean-Reversion behavioral tests."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from stockscan.strategies import PositionSnapshot
from stockscan.strategies.rsi2_meanrev import RSI2MeanReversion, RSI2Params


def _make_bars(closes: list[float], symbol: str = "TEST") -> pd.DataFrame:
    n = len(closes)
    idx = pd.date_range("2024-01-01", periods=n, freq="B", tz="UTC")
    df = pd.DataFrame(
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
    return df


@pytest.fixture
def strategy() -> RSI2MeanReversion:
    return RSI2MeanReversion(RSI2Params())


def test_no_signal_in_downtrend(strategy):
    """Strategy MUST NOT signal when price is below SMA(200)."""
    # 250 days of declining prices: ends well below SMA(200).
    closes = list(np.linspace(200, 80, 250))
    bars = _make_bars(closes)
    sigs = strategy.signals(bars, as_of=bars.index[-1].date())
    assert sigs == []


def test_no_signal_when_rsi_above_threshold(strategy):
    """Pure uptrend → RSI ≈ 100; no entry."""
    closes = list(np.linspace(100, 200, 250))
    bars = _make_bars(closes)
    sigs = strategy.signals(bars, as_of=bars.index[-1].date())
    assert sigs == []


def test_signal_fires_on_pullback_in_uptrend(strategy):
    """Long uptrend then a sharp dip → RSI(2) low + close > SMA(200) → signal."""
    n_up = 240
    n_down = 5
    up = np.linspace(100, 200, n_up).tolist()
    down = np.linspace(200, 195, n_down).tolist()  # small dip preserves > SMA(200)
    bars = _make_bars(up + down)

    sigs = strategy.signals(bars, as_of=bars.index[-1].date())
    assert len(sigs) == 1
    sig = sigs[0]
    assert sig.symbol == "TEST"
    assert sig.side == "long"
    assert sig.suggested_stop < sig.suggested_entry
    assert sig.score > 0


def test_signals_idempotent_and_no_lookahead(strategy):
    closes = list(np.linspace(100, 200, 240)) + list(np.linspace(200, 196, 5))
    bars = _make_bars(closes)
    as_of = bars.index[-1].date()
    a = strategy.signals(bars.copy(), as_of)
    b = strategy.signals(bars.copy(), as_of)
    assert a == b
    # Truncating future bars must not change output (there are none after as_of here,
    # so truncation is a no-op — but exercise the path).
    truncated = bars[bars.index.date <= as_of]
    c = strategy.signals(truncated, as_of)
    assert a == c


def test_exit_time_stop_after_max_holding(strategy):
    """An open position should hit the time stop after max_holding_days bars."""
    closes = [100.0] * 30  # flat — no other exit triggers
    bars = _make_bars(closes)
    pos = PositionSnapshot(
        symbol="TEST",
        qty=10,
        avg_cost=Decimal("100"),
        opened_at=datetime(2024, 1, 1, 16, tzinfo=timezone.utc),
        strategy="rsi2_meanrev",
    )
    decision = strategy.exit_rules(pos, bars, as_of=bars.index[-1].date())
    assert decision is not None
    assert decision.reason == "time_stop"


def test_exit_mean_reversion_above_sma5(strategy):
    """Close > SMA(5) triggers a mean-reversion exit (with enough ATR warmup)."""
    # 16 flat days (gives ATR(14) one valid value), then a small pop above SMA(5).
    # Position opened on day 16 so we're not at the time stop.
    closes = [100.0] * 16 + [105.0] * 2
    bars = _make_bars(closes)
    open_day = bars.index[15].to_pydatetime()  # day 16 (0-indexed 15)
    pos = PositionSnapshot(
        symbol="TEST",
        qty=10,
        avg_cost=Decimal("100"),
        opened_at=open_day,
        strategy="rsi2_meanrev",
    )
    decision = strategy.exit_rules(pos, bars, as_of=bars.index[-1].date())
    assert decision is not None
    assert decision.reason == "mean_reverted_above_sma5"
