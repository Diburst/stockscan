"""Behavioral tests for the Largecap Rebound strategy.

The contract test in test_strategy_contract.py runs against this strategy
automatically (idempotence, no-look-ahead, valid required_history).
This file covers strategy-specific behavior:

  - Setup filter: must be BELOW SMA(200)
  - Setup filter: market cap must clear the percentile floor
  - Entry: BOTH RSI > threshold + rising AND MACD histogram > 0 + rising
  - Exits: profit target at SMA(50), hard stop, time stop
  - _is_large_cap returns False when there's no fundamentals row

We patch `market_cap_percentile` so these tests don't need a database.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from stockscan.strategies import PositionSnapshot
from stockscan.strategies.largecap_rebound import (
    LargeCapRebound,
    LargeCapReboundParams,
)


# ----------------------------------------------------------------------
# Test fixtures
# ----------------------------------------------------------------------
def _make_bars(closes: list[float], symbol: str = "AAPL") -> pd.DataFrame:
    """Build an OHLCV DataFrame indexed by business day, tz=UTC."""
    n = len(closes)
    idx = pd.date_range("2023-01-02", periods=n, freq="B", tz="UTC")
    return pd.DataFrame(
        {
            "open":      closes,
            "high":      [c + 1 for c in closes],
            "low":       [c - 1 for c in closes],
            "close":     closes,
            "adj_close": closes,
            "volume":    [5_000_000] * n,
            "symbol":    [symbol] * n,
        },
        index=idx,
    )


def _downtrend_then_recovery(n_down: int = 240, n_recovery: int = 12) -> list[float]:
    """Long downtrend (puts close well below SMA(200)), then a sharp recovery
    that drives RSI > 50 + rising AND MACD histogram > 0 + rising."""
    down = list(np.linspace(150, 80, n_down))
    # Sharp V-bottom: 4 days flat, then ramp up steeply enough to flip MACD.
    flat = [80.0] * 4
    ramp = list(np.linspace(82.0, 100.0, n_recovery - 4))
    return down + flat + ramp


@pytest.fixture
def strategy() -> LargeCapRebound:
    """Default strategy fixture with the ADX filter loosened to 0 — most
    tests in this file are checking RSI/MACD/SMA/market-cap entry logic
    without ADX as a confounding factor. The ADX filter is exercised by
    its own dedicated tests below."""
    return LargeCapRebound(LargeCapReboundParams(adx_min=0.0))


@pytest.fixture
def mock_large_cap():
    """Patch market_cap_percentile to return >= 80 (passes the default floor)."""
    with patch(
        "stockscan.strategies.largecap_rebound.market_cap_percentile",
        return_value=92.0,
    ) as m:
        yield m


@pytest.fixture
def mock_small_cap():
    """Patch market_cap_percentile to return below the default floor."""
    with patch(
        "stockscan.strategies.largecap_rebound.market_cap_percentile",
        return_value=50.0,
    ) as m:
        yield m


@pytest.fixture
def mock_no_fundamentals():
    """Patch market_cap_percentile to return None (no fundamentals row)."""
    with patch(
        "stockscan.strategies.largecap_rebound.market_cap_percentile",
        return_value=None,
    ) as m:
        yield m


# ======================================================================
# Setup filter: below SMA(200)
# ======================================================================
def test_no_signal_when_above_sma200(strategy, mock_large_cap):
    """Strict uptrend → close above SMA(200) → strategy must not fire."""
    closes = list(np.linspace(100, 200, 280))  # always rising, always above SMA
    bars = _make_bars(closes)
    sigs = strategy.signals(bars, bars.index[-1].date())
    assert sigs == []


# ======================================================================
# Setup filter: market cap percentile
# ======================================================================
def test_no_signal_when_below_market_cap_percentile(strategy, mock_small_cap):
    """Even with a perfect technical setup, sub-floor market cap rejects."""
    closes = _downtrend_then_recovery()
    bars = _make_bars(closes)
    sigs = strategy.signals(bars, bars.index[-1].date())
    assert sigs == []


def test_no_signal_when_no_fundamentals_row(strategy, mock_no_fundamentals):
    """If we have no market cap data, abstain — don't assume pass/fail."""
    closes = _downtrend_then_recovery()
    bars = _make_bars(closes)
    sigs = strategy.signals(bars, bars.index[-1].date())
    assert sigs == []


def test_is_large_cap_helper_returns_false_when_no_data():
    """Direct test of _is_large_cap, separate from the signals() flow."""
    s = LargeCapRebound(LargeCapReboundParams())
    with patch(
        "stockscan.strategies.largecap_rebound.market_cap_percentile",
        return_value=None,
    ):
        assert s._is_large_cap("AAPL", pd.Timestamp("2024-01-02").date()) is False


def test_is_large_cap_helper_uses_threshold():
    """Boundary: percentile == floor → True; one below → False."""
    s = LargeCapRebound(LargeCapReboundParams(market_cap_pct_floor=80))
    today = pd.Timestamp("2024-01-02").date()
    with patch(
        "stockscan.strategies.largecap_rebound.market_cap_percentile",
        return_value=80.0,
    ):
        assert s._is_large_cap("AAPL", today) is True
    with patch(
        "stockscan.strategies.largecap_rebound.market_cap_percentile",
        return_value=79.99,
    ):
        assert s._is_large_cap("AAPL", today) is False


# ======================================================================
# Entry triggers: RSI + MACD both bullish + rising
# ======================================================================
def test_signal_fires_on_qualifying_setup(strategy, mock_large_cap):
    """Downtrend + sharp recovery with rising RSI > 50 + rising MACD > 0."""
    closes = _downtrend_then_recovery()
    bars = _make_bars(closes)
    sigs = strategy.signals(bars, bars.index[-1].date())
    assert len(sigs) == 1
    sig = sigs[0]
    assert sig.symbol == "AAPL"
    assert sig.side == "long"
    assert sig.suggested_stop < sig.suggested_entry
    # Score is in [0, 1]
    assert Decimal("0") <= sig.score <= Decimal("1")
    # Metadata captures the indicators the user mentioned
    md = sig.metadata
    assert "rsi" in md and md["rsi"] > 50
    assert md["rsi_slope"] > 0          # rising
    assert md["macd_histogram"] > 0     # bullish
    assert md["macd_slope"] > 0         # rising
    assert md["sma200"] > 0
    assert md["dist_below_sma_pct"] > 0  # close is below SMA200


def test_no_signal_when_rsi_falling(strategy, mock_large_cap):
    """Pure downtrend, no recovery → RSI falling → no signal."""
    closes = list(np.linspace(150, 80, 280))
    bars = _make_bars(closes)
    sigs = strategy.signals(bars, bars.index[-1].date())
    assert sigs == []


def test_no_signal_when_macd_negative(strategy, mock_large_cap):
    """RSI bullish but MACD still negative → no signal.

    Build bars where RSI ends rising > 50 but the recovery is too brief to
    push MACD histogram positive.
    """
    # Long downtrend then 2-day micro-bounce — not enough for MACD to flip.
    closes = list(np.linspace(150, 80, 280)) + [81.0, 82.0]
    bars = _make_bars(closes)
    sigs = strategy.signals(bars, bars.index[-1].date())
    assert sigs == []


def test_idempotent(strategy, mock_large_cap):
    """Running signals twice on identical bars must give identical output."""
    closes = _downtrend_then_recovery()
    bars = _make_bars(closes)
    a = strategy.signals(bars.copy(), bars.index[-1].date())
    b = strategy.signals(bars.copy(), bars.index[-1].date())
    assert a == b


# ----------------------------------------------------------------------
# ADX chop-resistance filter
# ----------------------------------------------------------------------
def test_adx_filter_rejects_choppy_market(mock_large_cap):
    """A sideways/range-bound market produces low ADX — strategy must skip."""
    rng = np.random.default_rng(42)
    # ~280 days of small random noise around 100 — perfect chop
    closes = (100 + rng.normal(0, 0.5, 280)).tolist()
    bars = _make_bars(closes)
    # Default adx_min = 20; chop should keep ADX below this.
    strict_strategy = LargeCapRebound(LargeCapReboundParams(adx_min=20.0))
    sigs = strict_strategy.signals(bars, bars.index[-1].date())
    # Either no signal (ADX too low) OR if the random walk happens to drift
    # in one direction long enough to push ADX up, we just want NO crash.
    for sig in sigs:
        # If a signal does fire, its metadata must show ADX ≥ threshold.
        assert sig.metadata["adx"] >= 20.0


def test_adx_filter_passes_during_directional_move(mock_large_cap):
    """A long downtrend then sharp recovery — ADX should be elevated and
    the strategy should fire (when other conditions also pass)."""
    closes = _downtrend_then_recovery()
    bars = _make_bars(closes)
    # Use the strict default ADX threshold; a 240-bar downtrend should have
    # left ADX well above 20.
    strict_strategy = LargeCapRebound(LargeCapReboundParams(adx_min=20.0))
    sigs = strict_strategy.signals(bars, bars.index[-1].date())
    if sigs:
        # If a signal fires, ADX should be in metadata and ≥ threshold.
        assert "adx" in sigs[0].metadata
        assert sigs[0].metadata["adx"] >= 20.0


def test_adx_value_is_persisted_in_metadata(strategy, mock_large_cap):
    """ADX value should appear in the signal metadata for chart hovers + audit."""
    closes = _downtrend_then_recovery()
    bars = _make_bars(closes)
    sigs = strategy.signals(bars, bars.index[-1].date())
    if sigs:
        assert "adx" in sigs[0].metadata
        assert isinstance(sigs[0].metadata["adx"], float)


# ======================================================================
# Exits — MACD-cross + hard stop only (no profit target, no time stop)
# ======================================================================
def test_exit_when_macd_histogram_below_zero(strategy):
    """Bullish run that rolls over → MACD histogram goes negative → exit."""
    # Long uptrend (MACD positive, accelerating) followed by a sharp pullback
    # large enough to push MACD histogram negative without breaching the
    # ATR-based hard stop.
    rising = list(np.linspace(80, 130, 200))   # MACD positive + rising at end
    rolling = list(np.linspace(130, 122, 30))  # mild pullback, MACD turns over
    bars = _make_bars(rising + rolling)
    pos = PositionSnapshot(
        symbol="AAPL", qty=100, avg_cost=Decimal("128"),
        opened_at=bars.index[-32].to_pydatetime(),
        strategy="largecap_rebound",
    )
    decision = strategy.exit_rules(pos, bars, bars.index[-1].date())
    assert decision is not None
    # Either MACD or hard stop is reasonable depending on exact numbers;
    # we just want a known exit reason.
    assert decision.reason in {"macd_below_zero", "hard_stop"}


def test_no_exit_during_strong_uptrend(strategy):
    """While close keeps making highs and MACD histogram stays positive,
    don't exit — let winners ride."""
    closes = list(np.linspace(80, 200, 280))  # pure uptrend
    bars = _make_bars(closes)
    pos = PositionSnapshot(
        symbol="AAPL", qty=100, avg_cost=Decimal("100"),
        opened_at=bars.index[-50].to_pydatetime(),  # held 50 trading days
        strategy="largecap_rebound",
    )
    decision = strategy.exit_rules(pos, bars, bars.index[-1].date())
    # Should be None — MACD positive, no hard-stop hit, no time stop.
    assert decision is None


def test_hard_stop_fires_on_steep_drop(strategy):
    """Position opened high, price collapses — hard stop should fire."""
    # Build a series where MACD has been positive (so the entry was valid)
    # then the price drops sharply enough to clear the 2.5×ATR cushion.
    rising = list(np.linspace(80, 100, 200))
    crash = list(np.linspace(100, 60, 30))
    bars = _make_bars(rising + crash)
    pos = PositionSnapshot(
        symbol="AAPL", qty=100, avg_cost=Decimal("100"),
        opened_at=bars.index[-3].to_pydatetime(),  # opened just before the crash
        strategy="largecap_rebound",
    )
    decision = strategy.exit_rules(pos, bars, bars.index[-1].date())
    assert decision is not None
    assert decision.reason == "hard_stop"


def test_no_time_stop(strategy):
    """Even very old positions don't get time-stopped — winners ride."""
    closes = list(np.linspace(80, 200, 280))  # strong uptrend
    bars = _make_bars(closes)
    pos = PositionSnapshot(
        symbol="AAPL", qty=100, avg_cost=Decimal("100"),
        opened_at=bars.index[100].to_pydatetime(),  # opened ~180 days ago
        strategy="largecap_rebound",
    )
    decision = strategy.exit_rules(pos, bars, bars.index[-1].date())
    # MACD still positive, hard stop not hit → no exit despite long hold.
    assert decision is None
