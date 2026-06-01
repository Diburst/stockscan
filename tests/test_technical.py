"""Reversal scoring (strategy-owned) + MACD math sanity.

The per-strategy, tag-aware RSI/MACD "confirmation" indicators have been retired:
indicators are now strategy-agnostic primitives in ``stockscan.indicators``, and
each strategy owns its own scoring logic inline. This module covers the reversal
score's signed/bounded behavior (sourced through ``ReversalSwing.reversal_score``
— the single home of that math) and the MACD primitive's algebra. The reversal
building-block primitives themselves are unit-tested in test_reversal_indicators.py.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from stockscan.indicators import macd as compute_macd
from stockscan.strategies.reversal_swing import ReversalSwing


def _bars_with_constant_then_dip(n_flat: int = 60, n_dip: int = 4) -> pd.DataFrame:
    """Flat with a historical V-low, then a dip into oversold, then a hook.

    The fixture models a full v1.4.0-era "confirmed bottom":

      - hook bar (last) closes above prev → positive reversal_trigger
      - earlier V-shape creates a confirmed swing low BELOW the eventual hook
        close → positive pivot_proximity (gate at >0 passes)

    Without the V-shape, the strategy's third gate (added v1.4.0) would reject
    the setup as "buying near recent extremes without a confirmed support to
    lean on" — which is dip-buying, not reversal trading.
    """
    closes = [100.0] * n_flat + list(np.linspace(100, 92, n_dip))
    # Insert a confirmed swing pivot inside the trailing 60-bar lookback the
    # pivot_proximity primitive uses. The 7-bar V [98, 96, 95, 94, 95, 96, 98]
    # confirms a swing low at index 25 (close 94, low 93) with k=3 higher
    # lows on each side. The eventual hook close (95) is close enough to this
    # support level in ATR units that pivot_proximity registers strongly
    # positive — modelling the "buy at a confirmed level" structural pattern.
    closes[22:29] = [98.0, 96.0, 95.0, 94.0, 95.0, 96.0, 98.0]
    closes[-1] = 95.0  # hook back up — last > prev so reversal_trigger fires positive
    n = len(closes)
    idx = pd.date_range("2024-01-02", periods=n, freq="B", tz="UTC")
    return pd.DataFrame({
        "open": closes,
        "high": [c + 1 for c in closes],
        "low": [c - 1 for c in closes],
        "close": closes,
        "adj_close": closes,
        "volume": [1_000_000] * n,
    }, index=idx)


def _bars_with_uptrend(n: int = 80) -> pd.DataFrame:
    closes = list(np.linspace(80, 130, n))
    idx = pd.date_range("2024-01-02", periods=n, freq="B", tz="UTC")
    return pd.DataFrame({
        "open": closes,
        "high": [c + 1 for c in closes],
        "low": [c - 1 for c in closes],
        "close": closes,
        "adj_close": closes,
        "volume": [1_000_000] * n,
    }, index=idx)


def _strat() -> ReversalSwing:
    return ReversalSwing()


# ----------------------------------------------------------------------
# MACD math sanity (the primitive in indicators/ta.py — single canonical impl)
# ----------------------------------------------------------------------
def test_macd_math_returns_three_columns():
    closes = pd.Series(np.linspace(100, 130, 100))
    df = compute_macd(closes, 12, 26, 9)
    assert set(df.columns) == {"macd", "signal", "histogram"}
    assert len(df) == 100


def test_macd_histogram_definition():
    """histogram == macd − signal, by construction."""
    closes = pd.Series(np.linspace(100, 130, 100))
    df = compute_macd(closes, 12, 26, 9).dropna()
    diffs = (df["macd"] - df["signal"]) - df["histogram"]
    assert diffs.abs().max() < 1e-9


# ----------------------------------------------------------------------
# Reversal score (strategy-owned)
# ----------------------------------------------------------------------
def test_score_uses_reversal_primitives():
    bars = _bars_with_constant_then_dip(60, 4)
    result = _strat().reversal_score(bars, bars.index[-1].date())
    assert result is not None
    assert -1 <= result.score <= 1
    # reversal primitives contribute; legacy rsi/macd are not part of the score.
    assert "reversal_trigger" in result.breakdown
    assert "rsi" not in result.breakdown
    assert "macd" not in result.breakdown
    assert result.breakdown["_meta"]["methodology_version"] == 2


def test_score_returns_none_when_no_turn():
    """v1.2.0 hard gate: a monotonic uptrend (no bottom hook) → score is None."""
    bars = _bars_with_uptrend(80)
    assert _strat().reversal_score(bars, bars.index[-1].date()) is None


def test_score_returns_none_when_no_pivot():
    """v1.4.0 hard gate: a setup with a real bottom turn but NO confirmed
    swing support below price → score is None. Same shape as test_technical's
    constant_then_dip fixture but WITHOUT the V-low — i.e. firing "near a
    recent extreme" with nothing structural to lean on. This is the bt23
    failure mode the gate is designed to filter."""
    closes = [100.0] * 60 + list(np.linspace(100, 92, 4))
    closes[-1] = 95.0  # hook produces positive reversal_trigger
    # NOTE: no V-shape inserted — there's no confirmed swing low below 95
    # anywhere in the trailing 60-bar window, so pivot_proximity returns 0.
    n = len(closes)
    idx = pd.date_range("2024-01-02", periods=n, freq="B", tz="UTC")
    bars = pd.DataFrame({
        "open": closes, "high": [c + 1 for c in closes], "low": [c - 1 for c in closes],
        "close": closes, "adj_close": closes, "volume": [1_000_000] * n,
    }, index=idx)
    assert _strat().reversal_score(bars, bars.index[-1].date()) is None


def test_score_returns_none_on_empty_bars():
    assert _strat().reversal_score(pd.DataFrame(), date(2024, 1, 8)) is None


def test_score_is_signed_and_bounded():
    """The reversal score is signed and in [-1, 1]. It's strategy-agnostic: the
    only data-dependent input, sector relative strength, abstains without a DB in
    unit tests, so the score here is built from the bars-only primitives."""
    bars = _bars_with_constant_then_dip(60, 4)
    as_of = bars.index[-1].date()
    r = _strat().reversal_score(bars, as_of)
    assert r is not None
    assert -1.0 <= r.score <= 1.0
    assert r.breakdown["_meta"]["methodology_version"] == 2


def test_score_breakdown_round_trips_to_dict():
    bars = _bars_with_constant_then_dip(60, 4)
    result = _strat().reversal_score(bars, bars.index[-1].date())
    payload = result.to_breakdown_json()
    assert "score" in payload
    assert "indicators" in payload
    assert isinstance(payload["indicators"], dict)
