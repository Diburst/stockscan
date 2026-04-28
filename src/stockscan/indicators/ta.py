"""Pure pandas/numpy indicator implementations.

Conventions:
  - Inputs are pandas Series or DataFrames indexed by datetime (asc).
  - Outputs are Series/DataFrames aligned to the input index.
  - Insufficient-history positions are NaN, never zero or fill-forward.
  - Wilder smoothing (used in RSI, ATR, ADX) implemented as the standard
    recursive EMA with alpha = 1/period (NOT the pandas ewm default).
"""

from __future__ import annotations

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------
# Moving averages
# ---------------------------------------------------------------------
def sma(series: pd.Series, period: int) -> pd.Series:
    """Simple moving average."""
    return series.rolling(window=period, min_periods=period).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential moving average (pandas adjust=False)."""
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def _wilder_smoothing(series: pd.Series, period: int) -> pd.Series:
    """Wilder's smoothing — recursive EMA with alpha = 1/period.

    The first value is the simple mean of the first `period` observations;
    subsequent values use the recursion: y[t] = y[t-1] + (x[t] - y[t-1]) / period.
    Matches the canonical RSI / ATR / ADX behavior used in TA literature.
    """
    s = series.astype(float).copy()
    out = pd.Series(np.nan, index=s.index, dtype=float)
    if len(s) < period:
        return out
    # Seed with simple mean of first `period` values.
    seed = s.iloc[:period].mean()
    out.iloc[period - 1] = seed
    prev = seed
    values = s.iloc[period:].to_numpy()
    for i, v in enumerate(values, start=period):
        prev = prev + (v - prev) / period
        out.iloc[i] = prev
    return out


# ---------------------------------------------------------------------
# RSI (Wilder)
# ---------------------------------------------------------------------
def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Relative Strength Index, Wilder's smoothing.

    Returns values in [0, 100]; NaN for the first `period` rows.
    """
    delta = close.astype(float).diff()
    gains = delta.clip(lower=0.0)
    losses = -delta.clip(upper=0.0)

    avg_gain = _wilder_smoothing(gains, period)
    avg_loss = _wilder_smoothing(losses, period)

    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    # Special case: zero loss over the window → RSI = 100 (saturated up).
    # Zero gain → RSI = 0.
    out = out.where(avg_loss > 0, 100.0)
    out = out.where(avg_gain > 0, out.where(avg_loss == 0, 0.0))
    return out


# ---------------------------------------------------------------------
# True Range / ATR
# ---------------------------------------------------------------------
def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """True Range: max(H-L, |H - prev close|, |L - prev close|)."""
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    return pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Average True Range, Wilder smoothing."""
    tr = true_range(high, low, close)
    return _wilder_smoothing(tr, period)


# ---------------------------------------------------------------------
# Donchian channel
# ---------------------------------------------------------------------
def donchian_channel(
    high: pd.Series,
    low: pd.Series,
    period: int = 20,
) -> pd.DataFrame:
    """Donchian channel: rolling max of high, rolling min of low, midpoint.

    Returns a DataFrame with columns [upper, lower, middle].
    The current bar is INCLUDED in the window — for "is today a new
    20-day high?" tests, compare today's close to `upper.shift(1)`.
    """
    upper = high.rolling(window=period, min_periods=period).max()
    lower = low.rolling(window=period, min_periods=period).min()
    middle = (upper + lower) / 2
    return pd.DataFrame({"upper": upper, "lower": lower, "middle": middle})


# ---------------------------------------------------------------------
# ADX (Average Directional Index)
# ---------------------------------------------------------------------
def adx(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 14,
) -> pd.Series:
    """ADX with Wilder smoothing. Range [0, 100]; >25 = strong trend."""
    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = pd.Series(
        np.where((up_move > down_move) & (up_move > 0), up_move, 0.0),
        index=high.index,
    )
    minus_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0), down_move, 0.0),
        index=high.index,
    )

    tr = true_range(high, low, close)

    atr_w = _wilder_smoothing(tr, period)
    plus_di = 100 * _wilder_smoothing(plus_dm, period) / atr_w.replace(0, np.nan)
    minus_di = 100 * _wilder_smoothing(minus_dm, period) / atr_w.replace(0, np.nan)

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return _wilder_smoothing(dx.fillna(0.0), period)


# ---------------------------------------------------------------------
# MACD — Moving Average Convergence Divergence
# ---------------------------------------------------------------------
def macd(
    close: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> pd.DataFrame:
    """MACD with the canonical 12/26/9 EMA periods.

    Returns a DataFrame with columns:
      - macd      : EMA(fast) − EMA(slow)
      - signal    : EMA(macd, signal)
      - histogram : macd − signal

    Histogram > 0 + rising = bullish acceleration;
    histogram < 0 + falling = bearish acceleration.
    """
    ema_fast = ema(close, fast)
    ema_slow = ema(close, slow)
    macd_line = ema_fast - ema_slow
    signal_line = ema(macd_line, signal)
    histogram = macd_line - signal_line
    return pd.DataFrame(
        {"macd": macd_line, "signal": signal_line, "histogram": histogram}
    )


# ---------------------------------------------------------------------
# Bollinger Bands
# ---------------------------------------------------------------------
def bollinger_bands(close: pd.Series, period: int = 20, stddev: float = 2.0) -> pd.DataFrame:
    """Bollinger bands: SMA ± stddev × rolling std."""
    middle = sma(close, period)
    std = close.rolling(window=period, min_periods=period).std(ddof=0)
    upper = middle + stddev * std
    lower = middle - stddev * std
    return pd.DataFrame({"upper": upper, "middle": middle, "lower": lower})


# ---------------------------------------------------------------------
# Liquidity
# ---------------------------------------------------------------------
def avg_dollar_volume(close: pd.Series, volume: pd.Series, period: int = 20) -> pd.Series:
    """Rolling average of close × volume — the liquidity floor used by filters."""
    return (close.astype(float) * volume.astype(float)).rolling(
        window=period, min_periods=period
    ).mean()
