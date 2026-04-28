"""Indicator math sanity tests.

We're not chasing perfect agreement with any particular library here —
we're verifying the math matches the standard definitions and is well-behaved
on synthetic and real-shaped data.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from stockscan.indicators import (
    adx,
    atr,
    avg_dollar_volume,
    bollinger_bands,
    donchian_channel,
    ema,
    rsi,
    sma,
    true_range,
)


@pytest.fixture
def trend_up() -> pd.DataFrame:
    """Steadily rising price series — RSI should be ~100, ADX should rise."""
    n = 100
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    close = pd.Series(np.linspace(100, 200, n), index=idx)
    high = close + 1
    low = close - 1
    return pd.DataFrame({"open": close, "high": high, "low": low, "close": close})


@pytest.fixture
def random_walk() -> pd.DataFrame:
    rng = np.random.default_rng(42)
    n = 250
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    rets = rng.normal(0, 0.01, n)
    close = pd.Series(100 * np.exp(np.cumsum(rets)), index=idx)
    high = close * 1.005
    low = close * 0.995
    vol = pd.Series(rng.integers(1_000_000, 5_000_000, n), index=idx)
    return pd.DataFrame({"open": close, "high": high, "low": low, "close": close, "volume": vol})


# --------------- SMA / EMA ---------------
def test_sma_basic():
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    out = sma(s, 3)
    assert pd.isna(out.iloc[0])
    assert pd.isna(out.iloc[1])
    assert out.iloc[2] == 2.0
    assert out.iloc[3] == 3.0
    assert out.iloc[4] == 4.0


def test_ema_starts_with_nan_warmup():
    s = pd.Series(range(20), dtype=float)
    out = ema(s, 5)
    # First 4 values NaN (need 5 periods); from index 4 onward populated.
    assert pd.isna(out.iloc[3])
    assert not pd.isna(out.iloc[4])


# --------------- RSI ---------------
def test_rsi_pure_uptrend_saturates_to_100(trend_up):
    r = rsi(trend_up["close"], 14)
    assert r.iloc[-1] == pytest.approx(100.0, abs=1e-6)


def test_rsi_pure_downtrend_saturates_to_0():
    s = pd.Series(np.linspace(200, 100, 100))
    r = rsi(s, 14)
    assert r.iloc[-1] == pytest.approx(0.0, abs=1e-6)


def test_rsi_in_range(random_walk):
    r = rsi(random_walk["close"], 14)
    valid = r.dropna()
    assert (valid >= 0).all() and (valid <= 100).all()


def test_rsi2_responds_quickly_to_pullback():
    # Price rises for 30 days then drops sharply for 3.
    n_up, n_down = 30, 3
    up = np.linspace(100, 130, n_up)
    down = np.linspace(130, 110, n_down)
    s = pd.Series(np.concatenate([up, down]))
    r = rsi(s, 2)
    # Last value (post-drop) should be very low.
    assert r.iloc[-1] < 25


# --------------- ATR ---------------
def test_atr_positive_in_random_walk(random_walk):
    a = atr(random_walk["high"], random_walk["low"], random_walk["close"], 14)
    valid = a.dropna()
    assert len(valid) > 0
    assert (valid > 0).all()


def test_true_range_nonneg(random_walk):
    tr = true_range(random_walk["high"], random_walk["low"], random_walk["close"])
    assert (tr.dropna() >= 0).all()


# --------------- Donchian ---------------
def test_donchian_uptrend(trend_up):
    chan = donchian_channel(trend_up["high"], trend_up["low"], 20)
    # In a strict uptrend the upper band should equal today's high.
    assert chan["upper"].iloc[-1] == pytest.approx(float(trend_up["high"].iloc[-1]))
    # Middle is between upper and lower.
    assert chan["lower"].iloc[-1] < chan["middle"].iloc[-1] < chan["upper"].iloc[-1]


# --------------- ADX ---------------
def test_adx_strong_in_pure_trend(trend_up):
    a = adx(trend_up["high"], trend_up["low"], trend_up["close"], 14)
    valid = a.dropna()
    # In a pure linear uptrend ADX should rise to a high value (>50 typical).
    assert valid.iloc[-1] > 30


def test_adx_in_range(random_walk):
    a = adx(random_walk["high"], random_walk["low"], random_walk["close"], 14)
    valid = a.dropna()
    assert (valid >= 0).all() and (valid <= 100).all()


# --------------- Bollinger ---------------
def test_bollinger_upper_above_middle(random_walk):
    bb = bollinger_bands(random_walk["close"], 20, 2.0)
    valid = bb.dropna()
    assert (valid["upper"] >= valid["middle"]).all()
    assert (valid["middle"] >= valid["lower"]).all()


# --------------- Volume ---------------
def test_avg_dollar_volume(random_walk):
    adv = avg_dollar_volume(random_walk["close"], random_walk["volume"], 20)
    valid = adv.dropna()
    assert (valid > 0).all()
