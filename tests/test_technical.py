"""Technical confirmation scoring — RSI + MACD per-strategy branches.

Covers:
  - Tag-aware scoring (mean_reversion vs trend_following branches)
  - Neutral mode (strategy=None) for the watchlist
  - Composite averaging
  - Score clamping to [-1, +1]
  - Insufficient-history → None values → indicator abstains
  - Unknown tags → 0 (composite still works, just lower weight)
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from stockscan.indicators import macd as compute_macd
from stockscan.technical.indicators.macd import MACDTechParams, TechnicalMACD
from stockscan.technical.indicators.rsi import RSITechParams, TechnicalRSI
from stockscan.technical.score import compute_technical_score


def _bars_with_constant_then_dip(n_flat: int = 60, n_dip: int = 4) -> pd.DataFrame:
    closes = [100.0] * n_flat + list(np.linspace(100, 92, n_dip))
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


def _strategy(*tags: str) -> SimpleNamespace:
    return SimpleNamespace(name="fake", tags=tags)


# ----------------------------------------------------------------------
# RSI scoring
# ----------------------------------------------------------------------
def test_rsi_low_value_confirms_mean_reversion():
    """In a sharp pullback, RSI is low → confirms a mean-reversion entry."""
    bars = _bars_with_constant_then_dip(60, 4)
    rsi = TechnicalRSI()
    values = rsi.values(bars, bars.index[-1].date())
    assert values is not None
    assert values["value"] < 35  # actually oversold
    score = rsi.score(values, _strategy("mean_reversion", "long_only"))
    assert score > 0.4  # confirming


def test_rsi_low_value_contradicts_trend_following():
    """Same low RSI is bad for trend-following (momentum has died)."""
    bars = _bars_with_constant_then_dip(60, 4)
    rsi = TechnicalRSI()
    values = rsi.values(bars, bars.index[-1].date())
    score = rsi.score(values, _strategy("trend_following", "breakout"))
    assert score < 0  # contradicting


def test_rsi_high_value_confirms_trend_following():
    """In a sustained uptrend, RSI is high → confirms a breakout entry."""
    bars = _bars_with_uptrend(80)
    rsi = TechnicalRSI()
    values = rsi.values(bars, bars.index[-1].date())
    score = rsi.score(values, _strategy("trend_following", "breakout"))
    assert score > 0.5


def test_rsi_extreme_high_capped_for_trend():
    """RSI > 80 should NOT score +1 — extension risk caps the upside at 0.5."""
    rsi = TechnicalRSI()
    score = rsi.score({"value": 90}, _strategy("trend_following"))
    assert score == 0.5


def test_rsi_unknown_tags_returns_zero():
    rsi = TechnicalRSI()
    score = rsi.score({"value": 30}, _strategy("pairs", "experimental"))
    assert score == 0.0


def test_rsi_neutral_mode_signed_bias():
    """strategy=None → direction-agnostic: high RSI bullish, low RSI bearish."""
    rsi = TechnicalRSI()
    assert rsi.score({"value": 70}, None) > 0
    assert rsi.score({"value": 30}, None) < 0
    assert abs(rsi.score({"value": 50}, None)) < 0.05


def test_rsi_insufficient_history_returns_none():
    rsi = TechnicalRSI()
    # Only 5 bars, RSI(14) needs ≥ 19
    bars = pd.DataFrame({"close": [100, 101, 102, 103, 104]})
    bars.index = pd.date_range("2024-01-02", periods=5, freq="B", tz="UTC")
    assert rsi.values(bars, date(2024, 1, 8)) is None


def test_rsi_score_always_clamped():
    rsi = TechnicalRSI()
    # RSI=0 in mean-reversion mode would naively give +1.67; must clamp to +1
    s = rsi.score({"value": 0}, _strategy("mean_reversion"))
    assert -1 <= s <= 1


# ----------------------------------------------------------------------
# MACD scoring
# ----------------------------------------------------------------------
def test_macd_negative_rising_confirms_mean_reversion():
    """Histogram negative but turning up = pullback bottoming = +confirming."""
    macd = TechnicalMACD()
    values = {"macd": 0, "signal": 0, "histogram": -0.5, "histogram_prev": -0.8}
    score = macd.score(values, _strategy("mean_reversion"))
    assert score > 0.3


def test_macd_negative_falling_contradicts_mean_reversion():
    """Histogram still falling = no bottom in sight = contradicting."""
    macd = TechnicalMACD()
    values = {"macd": 0, "signal": 0, "histogram": -0.8, "histogram_prev": -0.5}
    score = macd.score(values, _strategy("mean_reversion"))
    assert score < 0


def test_macd_positive_rising_confirms_trend_following():
    macd = TechnicalMACD()
    values = {"macd": 0, "signal": 0, "histogram": 0.5, "histogram_prev": 0.3}
    score = macd.score(values, _strategy("trend_following"))
    assert score > 0.5


def test_macd_negative_falling_contradicts_trend():
    macd = TechnicalMACD()
    values = {"macd": 0, "signal": 0, "histogram": -0.4, "histogram_prev": -0.2}
    score = macd.score(values, _strategy("trend_following"))
    assert score < -0.5


def test_macd_neutral_mode():
    macd = TechnicalMACD()
    # Bullish: positive + rising
    s_bull = macd.score(
        {"macd": 0, "signal": 0, "histogram": 0.5, "histogram_prev": 0.3}, None
    )
    # Bearish: negative + falling
    s_bear = macd.score(
        {"macd": 0, "signal": 0, "histogram": -0.4, "histogram_prev": -0.2}, None
    )
    assert s_bull > 0
    assert s_bear < 0


def test_macd_score_clamped():
    macd = TechnicalMACD()
    s = macd.score(
        {"macd": 0, "signal": 0, "histogram": -10, "histogram_prev": -10.0001},
        _strategy("mean_reversion"),
    )
    assert -1 <= s <= 1


# ----------------------------------------------------------------------
# MACD math sanity (the function in indicators/ta.py)
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
# Composite scoring
# ----------------------------------------------------------------------
def test_composite_averages_indicator_scores():
    bars = _bars_with_uptrend(80)
    result = compute_technical_score(_strategy("trend_following"), bars, bars.index[-1].date())
    assert result is not None
    assert -1 <= result.score <= 1
    # Both indicators should have weighed in
    assert result.contributing >= 1
    assert "rsi" in result.breakdown


def test_composite_returns_none_on_empty_bars():
    bars = pd.DataFrame()
    result = compute_technical_score(_strategy("trend_following"), bars, date(2024, 1, 8))
    assert result is None


def test_composite_strategy_aware():
    """Same bars, different strategies → different composite scores."""
    bars = _bars_with_constant_then_dip(60, 4)
    as_of = bars.index[-1].date()
    mr = compute_technical_score(_strategy("mean_reversion"), bars, as_of)
    tf = compute_technical_score(_strategy("trend_following"), bars, as_of)
    assert mr is not None and tf is not None
    # Pullback environment confirms MR, contradicts TF
    assert mr.score > tf.score


def test_composite_neutral_mode():
    """strategy=None should produce a real score (bullish bias) without
    requiring any specific tag."""
    bars = _bars_with_uptrend(80)
    result = compute_technical_score(None, bars, bars.index[-1].date())
    assert result is not None
    assert result.score > 0  # uptrend → bullish bias


def test_composite_breakdown_round_trips_to_dict():
    bars = _bars_with_uptrend(80)
    result = compute_technical_score(_strategy("trend_following"), bars, bars.index[-1].date())
    payload = result.to_breakdown_json()
    assert "score" in payload
    assert "indicators" in payload
    assert isinstance(payload["indicators"], dict)
