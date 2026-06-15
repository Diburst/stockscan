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

    The recursion is sequential (each step depends on the previous), so it
    can't be vectorised — but it can be run on a NumPy ndarray instead of a
    pandas Series, skipping the per-write block-consolidation and
    chained-assignment plumbing that dominated cProfile (~80% of total
    backtest runtime on a 10-symbol × 1-year reversal_swing run, with
    pandas ``__setitem__`` accounting for 2.3M of 2.5M cumulative calls).
    Same math, same seed, same NaN propagation — just no pandas overhead
    in the hot loop. ~50× faster on this function at scale.
    """
    s = series.astype(float).to_numpy()
    n = s.shape[0]
    out = np.full(n, np.nan, dtype=float)
    if n < period:
        return pd.Series(out, index=series.index, dtype=float)
    # Seed with simple mean of first `period` values (skips NaN, matches the
    # previous pandas .mean() behaviour).
    seed = float(np.nanmean(s[:period]))
    out[period - 1] = seed
    prev = seed
    for i in range(period, n):
        prev = prev + (s[i] - prev) / period
        out[i] = prev
    return pd.Series(out, index=series.index, dtype=float)


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


# ---------------------------------------------------------------------
# Realized volatility — Yang-Zhang (OHLC range estimator)
# ---------------------------------------------------------------------
# Close-to-close vol throws away the high and low of every bar. Yang-Zhang
# (2000) combines three pieces — the overnight gap (open vs prior close),
# the open-to-close drift, and the drift-free Rogers-Satchell intraday
# range — into one estimator that is both drift-independent and handles
# the overnight jumps single stocks gap on. At equal sample size it is
# several times more statistically efficient than close-to-close, so the
# same bars yield a steadier, less noisy σ.
#
# Both functions return *annualised* vol as a fraction (0.30 = 30%), a
# Series aligned to the input index with NaN warmup. Everything is
# vectorised (rolling / ewm run in C) — no per-cell Python loops.


def _yang_zhang_components(
    open_: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Per-bar Yang-Zhang inputs: (overnight, open-to-close, Rogers-Satchell).

    ``o`` and ``c`` are log returns whose *variance* feeds the overnight
    and open-close terms; ``rs`` is the per-bar Rogers-Satchell value whose
    *mean* feeds the intraday term. The overnight term needs the prior
    close, so the first row of ``o`` (and therefore every combined output)
    is NaN.
    """
    o_ = np.log(open_.astype(float))
    h_ = np.log(high.astype(float))
    l_ = np.log(low.astype(float))
    c_ = np.log(close.astype(float))
    prev_c = c_.shift(1)

    overnight = o_ - prev_c          # ln(O_t / C_{t-1})
    open_close = c_ - o_             # ln(C_t / O_t)
    u = h_ - o_                      # ln(H_t / O_t)
    d = l_ - o_                      # ln(L_t / O_t)
    rs = u * (u - open_close) + d * (d - open_close)  # Rogers-Satchell
    return overnight, open_close, rs


def _yang_zhang_k(n_eff: float) -> float:
    """Yang-Zhang weighting constant k for an (effective) sample size."""
    if n_eff <= 1:
        return 0.0
    return 0.34 / (1.34 + (n_eff + 1.0) / (n_eff - 1.0))


def yang_zhang_volatility(
    open_: pd.Series,
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    window: int,
    *,
    trading_periods: int = 252,
) -> pd.Series:
    """Rolling Yang-Zhang annualised volatility (fraction) over ``window`` bars."""
    overnight, open_close, rs = _yang_zhang_components(open_, high, low, close)
    # Overnight + open-close *variances*, Rogers-Satchell *mean*.
    v_o = overnight.rolling(window=window, min_periods=window).var(ddof=1)
    v_c = open_close.rolling(window=window, min_periods=window).var(ddof=1)
    v_rs = rs.rolling(window=window, min_periods=window).mean()
    k = _yang_zhang_k(float(window))
    yz_var = v_o + k * v_c + (1.0 - k) * v_rs
    yz_var = yz_var.clip(lower=0.0)
    return np.sqrt(yz_var * trading_periods)


def yang_zhang_volatility_ewm(
    open_: pd.Series,
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    *,
    lam: float = 0.94,
    trading_periods: int = 252,
) -> pd.Series:
    """EWMA Yang-Zhang annualised volatility (fraction), decay ``lam``.

    Exponentially weights the per-bar components (most-recent bar heaviest),
    the RiskMetrics convention. ``lam=0.94`` ≈ a 16-day centre of mass — a
    responsive, forward-leaning vol estimate. The weighting constant ``k``
    uses the EWMA effective sample size ``(1+lam)/(1-lam)``.
    """
    overnight, open_close, rs = _yang_zhang_components(open_, high, low, close)
    alpha = 1.0 - lam
    v_o = overnight.ewm(alpha=alpha, adjust=False).var(bias=False)
    v_c = open_close.ewm(alpha=alpha, adjust=False).var(bias=False)
    v_rs = rs.ewm(alpha=alpha, adjust=False).mean()
    n_eff = (1.0 + lam) / (1.0 - lam)
    k = _yang_zhang_k(n_eff)
    yz_var = v_o + k * v_c + (1.0 - k) * v_rs
    yz_var = yz_var.clip(lower=0.0)
    return np.sqrt(yz_var * trading_periods)
