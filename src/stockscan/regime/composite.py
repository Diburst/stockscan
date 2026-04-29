"""Pure component math for the v2 regime composite.

Every function here is a stateless transform from input ``pd.Series``
(or scalars) to output, so callers can replay them over historical data
without touching the DB, the providers, or any global state.

**No-look-ahead is the load-bearing invariant.** All percentile and
slope computations use *trailing* rolling windows only. Recomputing any
of these on a truncated copy of the input must match the live value at
that truncation point — there is a property test for this in
``tests/test_regime_composite.py`` and reviewers should treat that test
as the canonical safety net for the whole module.

Component conventions:

* All component scores are in ``[0, 1]`` where ``1.0`` = "healthy / calm"
  (the regime axis we'd prefer to be on).
* All windows default to 252 trading days (≈ 1 year) per the research
  doc §6.1.
* NaN propagates through warmup periods (first N-1 bars per window).
* :func:`composite_score_series` renormalizes weights over the non-NaN
  components per row, so a degraded data source (FRED down → credit
  NaN) doesn't kill the composite — it just shifts the weighting onto
  whatever's still available.

Weights (research doc §4.1):
    vol 0.40, trend 0.25, breadth 0.20, credit 0.15
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

# ----------------------------------------------------------------------
# Default tuning — exposed so tests / config can override
# ----------------------------------------------------------------------
DEFAULT_WINDOW = 252  # 1 year of trading days

# Trend score saturation bands. Position relative to SMA200 saturates at
# ±5% (so anything past 5% above/below is treated the same); 20-day SMA
# slope saturates at ±2% per 20 days.
TREND_POSITION_BAND = 0.05
TREND_SLOPE_BAND = 0.02
TREND_SLOPE_WINDOW = 20

# Breadth score: compare 20d vs 200d SMA of the RSP/SPY ratio.
# Saturate the relative gap at ±5%.
BREADTH_SHORT_WINDOW = 20
BREADTH_LONG_WINDOW = 200
BREADTH_BAND = 0.05

# Credit-stress flag: HY OAS percentile rank threshold, plus the lookback
# we use to check whether spreads are RISING (research doc §Tier 0(b)).
CREDIT_STRESS_RANK_THRESHOLD = 0.85
CREDIT_STRESS_LOOKBACK = 5

# Composite weights — exposed as a tuple so callers can override but we
# default to the research doc's recommended split.
DEFAULT_WEIGHTS: tuple[float, float, float, float] = (0.40, 0.25, 0.20, 0.15)


# ----------------------------------------------------------------------
# Vol score
# ----------------------------------------------------------------------
def vol_score(vix: pd.Series, window: int = DEFAULT_WINDOW) -> pd.Series:
    """Inverted rolling percentile rank of VIX.

    ``1 - rank`` so low VIX (calm market) maps to a high score. The
    rolling rank uses ``min_periods=window`` so early bars (before we
    have a full year of history) come back NaN rather than as biased
    short-window ranks.
    """
    rank = vix.rolling(window=window, min_periods=window).rank(pct=True)
    return (1.0 - rank).rename("vol_score")


# ----------------------------------------------------------------------
# Trend score
# ----------------------------------------------------------------------
def trend_score(
    close: pd.Series,
    sma200: pd.Series,
    *,
    position_band: float = TREND_POSITION_BAND,
    slope_band: float = TREND_SLOPE_BAND,
    slope_window: int = TREND_SLOPE_WINDOW,
) -> pd.Series:
    """Smooth combination of position-relative-to-SMA(200) and SMA(200) slope.

    Two signals, each clipped to ``[-1, +1]`` and averaged:

      * **Position**: ``(close - sma200) / sma200``, saturated at
        ±``position_band`` (default ±5%). Negative when below SMA200.
      * **Slope**: relative change in SMA200 over ``slope_window`` bars
        (default 20), saturated at ±``slope_band`` (default ±2%).

    Both saturation bands are calibrated on intuition rather than data —
    the doc warns against tuning these on the same window we backtest
    on, so they're deliberately fixed defaults.

    Mapped to ``[0, 1]`` via ``0.5 + 0.25 * (position + slope)``: 0.5 is
    "neutral", 1.0 is "strong uptrend on both axes", 0.0 is "strong
    downtrend on both axes".
    """
    position = ((close - sma200) / sma200 / position_band).clip(-1.0, 1.0)
    sma_lagged = sma200.shift(slope_window)
    slope = (sma200 - sma_lagged) / sma_lagged
    slope_norm = (slope / slope_band).clip(-1.0, 1.0)
    return (0.5 + 0.25 * (position + slope_norm)).clip(0.0, 1.0).rename("trend_score")


# ----------------------------------------------------------------------
# Breadth score (cheap RSP/SPY proxy)
# ----------------------------------------------------------------------
def breadth_score(
    rsp_close: pd.Series,
    spy_close: pd.Series,
    *,
    short: int = BREADTH_SHORT_WINDOW,
    long: int = BREADTH_LONG_WINDOW,
    band: float = BREADTH_BAND,
) -> pd.Series:
    """Equal-weight-vs-cap-weight breadth proxy.

    The ratio ``RSP / SPY`` rises when smaller-cap names lead and falls
    when the rally is concentrated in mega-caps (the "narrow rally"
    regime, e.g., the Mag 7 era). We compare a 20-day SMA of the ratio
    to its 200-day SMA: when 20d > 200d, breadth is broadening; when
    20d < 200d, breadth is narrowing.

    The relative gap is saturated at ±``band`` (default ±5%) and mapped
    to ``[0, 1]`` linearly, so 0.5 is "neither broadening nor narrowing".

    The research doc explicitly endorses this proxy as the cheap path
    for retail systems without per-symbol constituent data.
    """
    ratio = rsp_close / spy_close
    ratio_short = ratio.rolling(window=short, min_periods=short).mean()
    ratio_long = ratio.rolling(window=long, min_periods=long).mean()
    rel = (ratio_short - ratio_long) / ratio_long
    return (0.5 + 0.5 * (rel / band).clip(-1.0, 1.0)).clip(0.0, 1.0).rename("breadth_score")


# ----------------------------------------------------------------------
# Credit score + stress flag
# ----------------------------------------------------------------------
def credit_score(hy_oas: pd.Series, window: int = DEFAULT_WINDOW) -> pd.Series:
    """Inverted rolling percentile rank of HY OAS.

    ``1 - rank``: tight spreads (low OAS) → high score. Same trailing-
    window discipline as :func:`vol_score`.
    """
    rank = hy_oas.rolling(window=window, min_periods=window).rank(pct=True)
    return (1.0 - rank).rename("credit_score")


def credit_stress_flag(
    hy_oas: pd.Series,
    *,
    window: int = DEFAULT_WINDOW,
    rank_threshold: float = CREDIT_STRESS_RANK_THRESHOLD,
    lookback: int = CREDIT_STRESS_LOOKBACK,
) -> pd.Series:
    """Tail-risk circuit-breaker series, ``True`` on stress days.

    Fires when **both** are true:
      * HY OAS rolling percentile rank is above ``rank_threshold``
        (default 0.85 — top 15% of the trailing year).
      * HY OAS is rising over the last ``lookback`` bars (default 5).

    The flag is wired in the runner as a sizing override (research doc
    §Tier 0(b)): 0.5x size, no new long entries. It is intentionally
    *orthogonal* to ``credit_score`` (which stays a smooth 1 - rank);
    the flag is the discrete circuit breaker.

    Warmup bars (insufficient history for the rank or the lookback diff)
    return ``False`` rather than NaN so the result is a clean
    ``Series[bool]`` ready to use as a boolean mask.
    """
    rank = hy_oas.rolling(window=window, min_periods=window).rank(pct=True)
    rising = hy_oas > hy_oas.shift(lookback)
    flag = (rank > rank_threshold) & rising
    return flag.fillna(False).astype(bool).rename("credit_stress_flag")


def hy_oas_zscore(hy_oas: pd.Series, window: int = DEFAULT_WINDOW) -> pd.Series:
    """Trailing-window z-score of HY OAS.

    Persisted alongside the percentile rank for the dashboard banner —
    z-score makes it easier to read "how stretched is credit relative
    to its recent normal" at a glance.
    """
    mean = hy_oas.rolling(window=window, min_periods=window).mean()
    std = hy_oas.rolling(window=window, min_periods=window).std(ddof=0)
    # Avoid divide-by-zero in pathological constant-window stretches.
    z = (hy_oas - mean) / std.replace(0.0, np.nan)
    return z.rename("hy_oas_zscore")


# ----------------------------------------------------------------------
# Composite score — scalar (live use) and Series (replay)
# ----------------------------------------------------------------------
def composite_score(
    vol: float | None,
    trend: float | None,
    breadth: float | None,
    credit: float | None,
    *,
    weights: tuple[float, float, float, float] = DEFAULT_WEIGHTS,
) -> float | None:
    """Single-bar composite from four scalar components.

    Renormalizes weights over the non-NaN/non-None components, so a
    missing data source shifts weighting onto what's still available
    rather than killing the composite. Returns ``None`` only when every
    component is missing (the regime detector treats that as "no signal,
    fall back to neutral sizing" — see :mod:`stockscan.regime.detect`).
    """
    raw = (vol, trend, breadth, credit)
    valid: list[tuple[float, float]] = []
    for value, w in zip(raw, weights, strict=True):
        if value is None:
            continue
        if isinstance(value, float) and math.isnan(value):
            continue
        valid.append((float(value), w))
    if not valid:
        return None
    total_w = sum(w for _, w in valid)
    if total_w == 0.0:
        return None
    return sum(v * w for v, w in valid) / total_w


def composite_score_series(
    vol: pd.Series,
    trend: pd.Series,
    breadth: pd.Series,
    credit: pd.Series,
    *,
    weights: tuple[float, float, float, float] = DEFAULT_WEIGHTS,
) -> pd.Series:
    """Vectorized composite over four aligned Series.

    For each row, sums ``component * weight`` over the non-NaN
    components and divides by the sum of those components' weights.
    Equivalent to calling :func:`composite_score` per-bar, but does it
    in one pass over the join of the four input indices.

    Inputs are aligned via outer join; missing bars become NaN, which
    then participate in the per-row renormalization. Result is NaN only
    where every component is NaN.
    """
    df = pd.concat(
        [
            vol.rename("vol"),
            trend.rename("trend"),
            breadth.rename("breadth"),
            credit.rename("credit"),
        ],
        axis=1,
    )
    w = np.array(weights, dtype=float)
    mask = df.notna().to_numpy()
    vals = df.fillna(0.0).to_numpy()
    weighted_sum = vals @ w
    valid_weight_sum = mask.astype(float) @ w
    with np.errstate(invalid="ignore", divide="ignore"):
        out = np.where(valid_weight_sum > 0.0, weighted_sum / valid_weight_sum, np.nan)
    return pd.Series(out, index=df.index, name="composite_score")
