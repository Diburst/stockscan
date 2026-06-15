"""Volatility analysis + forward range projection.

Computes:

  * **Realized vol** (21d, 63d) - std of daily log returns, annualised
    via x √252. Industry standard "HV" computation.
  * **ATR(14)** in dollars + as % of current price - gives a more
    intraday-flavored volatility read than close-to-close vol.
  * **Bollinger band width** - (upper - lower) / middle, in %.
  * **HV percentile** - current 21-day realized vol's rank in its
    own trailing 252-day distribution. 0% = vol is at a 1-year low;
    100% = at a 1-year high. Substitutes for the IV-percentile
    framing options traders look for.
  * **Expected range projection** at 7d and 30d horizons - ±1sigma from
    current price using the 21d realized vol scaled by
    ``√(horizon / 252)``. NOT an option-implied move; it's a
    realized-vol-based estimate of the typical 7-day or 30-day
    fluctuation.

The bucket label translates the HV percentile + raw level into a
short interpretation usable for options strike selection.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import numpy as np

from stockscan.analysis.state import ExpectedRange, VolatilityState
from stockscan.indicators import (
    atr,
    bollinger_bands,
    yang_zhang_volatility,
    yang_zhang_volatility_ewm,
)

# EWMA decay for the forward (Yang-Zhang) vol estimate — RiskMetrics λ.
_EWMA_LAMBDA = 0.94

if TYPE_CHECKING:
    import pandas as pd


# Curated bucket labels keyed on HV percentile.
# (label, explanation) - color is derived elsewhere from bucket name.
_HV_BUCKETS: dict[str, tuple[str, str]] = {
    "low": (
        "Low (HV at 1-yr lows)",
        "Realized vol is in the bottom quartile of its trailing year. "
        "Premium is cheap; long-vol option strategies (long straddles, "
        "long strangles) tend to be more attractive here. Short-vol "
        "strategies face less premium to collect.",
    ),
    "normal": (
        "Normal",
        "Realized vol is in the middle band of its trailing year - "
        "no special vol setup. Most strike-selection should be driven "
        "by directional view + expected range rather than vol regime.",
    ),
    "elevated": (
        "Elevated",
        "Realized vol in the top quartile but not at extremes. "
        "Premium is rich on either side; short-vol strategies (credit "
        "spreads, iron condors) get higher payouts but the expected "
        "range is also wider.",
    ),
    "high": (
        "High (HV near 1-yr highs)",
        "Realized vol at or near a 1-year peak. Historically this "
        "precedes vol mean-reversion more often than continuation. "
        "Selling premium becomes attractive but check for known "
        "catalysts (earnings, macro events) that could justify it.",
    ),
}


def _hv_bucket(percentile: float) -> str:
    if percentile < 25:
        return "low"
    if percentile < 75:
        return "normal"
    if percentile < 90:
        return "elevated"
    return "high"


def compute_volatility(bars: pd.DataFrame) -> VolatilityState:
    """Run all volatility computations on a daily-bars DataFrame."""
    if bars is None or bars.empty or "close" not in bars.columns:
        return VolatilityState.unavailable()
    close = bars["close"]
    if len(close) < 22:
        return VolatilityState.unavailable()

    last_close = float(close.iloc[-1])
    if last_close <= 0:
        return VolatilityState.unavailable()

    # ---- OHLC for the Yang-Zhang range estimator ----
    open_ = bars.get("open")
    high = bars.get("high")
    low = bars.get("low")
    have_ohlc = open_ is not None and high is not None and low is not None

    # ---- Realized vol (annualised, %) ----
    # Yang-Zhang range estimator when OHLC is present (steadier, gap-aware);
    # close-to-close fallback when only closes exist.
    yz21_series = None
    if have_ohlc:
        yz21_series = yang_zhang_volatility(open_, high, low, close, 21) * 100.0
        yz63_series = yang_zhang_volatility(open_, high, low, close, 63) * 100.0
        rv_21 = _last_finite(yz21_series)
        rv_63 = _last_finite(yz63_series)
    else:
        log_returns_cc = np.log(close).diff().dropna()
        rv_21 = _annualised_vol(log_returns_cc, 21)
        rv_63 = _annualised_vol(log_returns_cc, 63)

    # ---- Forward vol: EWMA Yang-Zhang (λ=0.94) ----
    # Responsive, forward-leaning estimate. Drives BOTH the expected-move
    # bands and the option strike solver so the two never disagree. Falls
    # back to the trailing 21-day HV when OHLC/EWMA is unavailable.
    ewma_vol: float | None = None
    if have_ohlc:
        ewma_series = yang_zhang_volatility_ewm(
            open_, high, low, close, lam=_EWMA_LAMBDA
        ) * 100.0
        ewma_vol = _last_finite(ewma_series)
    forward_vol = ewma_vol if ewma_vol is not None else rv_21

    # ---- ATR(14) ----
    atr14: float | None = None
    if high is not None and low is not None and len(close) >= 21:
        atr_series = atr(high, low, close, 14)
        last_atr = atr_series.iloc[-1]
        if _is_finite(last_atr):
            atr14 = float(last_atr)

    atr_pct = (atr14 / last_close * 100) if (atr14 is not None and last_close > 0) else None

    # ---- Bollinger band width ----
    bb_width_pct: float | None = None
    if len(close) >= 21:
        bands = bollinger_bands(close, period=20, stddev=2.0)
        if not bands.empty:
            upper = bands["upper"].iloc[-1]
            lower = bands["lower"].iloc[-1]
            middle = bands["middle"].iloc[-1]
            if _is_finite(upper) and _is_finite(lower) and _is_finite(middle) and float(middle) > 0:
                bb_width_pct = float((upper - lower) / middle * 100)

    # ---- HV percentile ----
    # Rank today's 21-day Yang-Zhang HV against its own trailing year.
    hv_pct: float | None = None
    if yz21_series is not None:
        vol_series = yz21_series.dropna()
        if len(vol_series) >= 252:
            window = vol_series.iloc[-252:]
            today_v = float(window.iloc[-1])
            rank = (window <= today_v).sum()
            hv_pct = float(rank) / float(len(window)) * 100
    elif rv_21 is not None:
        log_returns_cc = np.log(close).diff().dropna()
        if len(log_returns_cc) >= 252 + 21:
            rolling_var = log_returns_cc.rolling(window=21, min_periods=21).var(ddof=1)
            rolling_vol_pct = (np.sqrt(rolling_var) * np.sqrt(252) * 100).dropna()
            if len(rolling_vol_pct) >= 252:
                window = rolling_vol_pct.iloc[-252:]
                today_v = float(window.iloc[-1])
                rank = (window <= today_v).sum()
                hv_pct = float(rank) / float(len(window)) * 100

    # ---- Expected range projection ----
    # Project the forward (EWMA-YZ) vol over 7 and 30 trading days.
    # sigma(horizon) = sigma(annual) x √(horizon / 252).
    expected_7d: ExpectedRange | None = None
    expected_30d: ExpectedRange | None = None
    if forward_vol is not None:
        for horizon, target in ((7, "expected_7d"), (30, "expected_30d")):
            sigma_pct = forward_vol * math.sqrt(horizon / 252.0)
            sigma_dollars = last_close * sigma_pct / 100
            er = ExpectedRange(
                horizon_days=horizon,
                sigma_pct=round(sigma_pct, 4),
                low=round(last_close * (1 - sigma_pct / 100), 4),
                high=round(last_close * (1 + sigma_pct / 100), 4),
                sigma_dollars=round(sigma_dollars, 4),
            )
            if target == "expected_7d":
                expected_7d = er
            else:
                expected_30d = er

    # ---- Bucket + label ----
    # Fallback to "normal" when we can't compute a percentile.
    bucket = _hv_bucket(hv_pct) if hv_pct is not None else "normal"
    label, explanation = _HV_BUCKETS.get(bucket, ("?", ""))

    return VolatilityState(
        available=True,
        realized_vol_21d_pct=round(rv_21, 4) if rv_21 is not None else None,
        realized_vol_63d_pct=round(rv_63, 4) if rv_63 is not None else None,
        atr_14=round(atr14, 4) if atr14 is not None else None,
        atr_pct_of_price=round(atr_pct, 4) if atr_pct is not None else None,
        bb_width_pct=round(bb_width_pct, 4) if bb_width_pct is not None else None,
        hv_percentile=round(hv_pct, 2) if hv_pct is not None else None,
        expected_7d=expected_7d,
        expected_30d=expected_30d,
        bucket=bucket,
        label=label,
        explanation=explanation,
        ewma_vol_pct=round(ewma_vol, 4) if ewma_vol is not None else None,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _annualised_vol(log_returns, window: int) -> float | None:
    """std(log_returns over window) x √252, expressed as a percent."""
    if len(log_returns) < window:
        return None
    sd = float(log_returns.iloc[-window:].std(ddof=1))
    if not _is_finite(sd) or sd <= 0:
        return None
    return sd * math.sqrt(252.0) * 100.0


def _is_finite(v: object) -> bool:
    try:
        return bool(np.isfinite(float(v)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False


def _last_finite(series) -> float | None:
    """Last finite value of a Series as a float, or None if none exists."""
    if series is None or len(series) == 0:
        return None
    s = series.dropna()
    if s.empty:
        return None
    last = float(s.iloc[-1])
    return last if _is_finite(last) and last > 0 else None
