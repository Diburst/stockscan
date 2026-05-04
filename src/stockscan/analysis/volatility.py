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
from stockscan.indicators import atr, bollinger_bands

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

    # ---- Realized vol (annualised) ----
    log_returns = np.log(close).diff().dropna()
    rv_21 = _annualised_vol(log_returns, 21)
    rv_63 = _annualised_vol(log_returns, 63)

    # ---- ATR(14) ----
    high = bars.get("high")
    low = bars.get("low")
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
    hv_pct: float | None = None
    if rv_21 is not None and len(log_returns) >= 252 + 21:
        # Build a rolling 21-day vol series and rank today against the trailing year.
        rolling_var = log_returns.rolling(window=21, min_periods=21).var(ddof=1)
        rolling_vol_pct = (np.sqrt(rolling_var) * np.sqrt(252) * 100).dropna()
        if len(rolling_vol_pct) >= 252:
            window = rolling_vol_pct.iloc[-252:]
            today_v = float(window.iloc[-1])
            rank = (window <= today_v).sum()
            hv_pct = float(rank) / float(len(window)) * 100

    # ---- Expected range projection ----
    # Project 21-day realized vol forward over 7 and 30 trading days.
    # sigma(horizon) = sigma(annual) x √(horizon / 252).
    expected_7d: ExpectedRange | None = None
    expected_30d: ExpectedRange | None = None
    if rv_21 is not None:
        for horizon, target in ((7, "expected_7d"), (30, "expected_30d")):
            sigma_pct = rv_21 * math.sqrt(horizon / 252.0)
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
