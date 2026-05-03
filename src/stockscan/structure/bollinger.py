"""SPY Bollinger Bands state — %B + width percentile, with interpretation.

Two readings rolled into one dataclass since they read the same bars:

  * **Bollinger %B** (Bollinger 1990s): position of today's close
    within the 20-day, 2-stddev bands. Above 1.0 = breaking above the
    upper band; below 0.0 = breaking below the lower band; 0.5 = at
    the middle (the 20-day SMA). Useful as a coincident overbought /
    oversold reading.

  * **BB width percentile**: today's BB width as a percentile rank
    of the trailing six-month distribution. Low percentile (<10) =
    volatility severely compressed (Crabel-style "coiled spring");
    expansion typically follows. High percentile (>90) = volatility
    at extremes; trends rarely sustain through this. The percentile
    framing is what makes the indicator comparable across regimes —
    raw width values are meaningless without context.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from stockscan.indicators import bollinger_bands

if TYPE_CHECKING:
    from datetime import date as _date

    import pandas as pd


# Lookback for the BB-width percentile. 126 trading days ≈ 6 months —
# the standard window in Crabel's Volatility Contraction Pattern work.
_BB_WIDTH_LOOKBACK = 126


# ---------------------------------------------------------------------------
# %B buckets
# ---------------------------------------------------------------------------

# (label, kind, explanation) for each bucket.
_PCT_B_BUCKETS: dict[str, tuple[str, str, str]] = {
    "below_lower": (
        "Below lower band",
        "ok",  # green for buy zone
        "%B below 0 means today's close pierced the lower band (the "
        "20-day SMA minus 2 standard deviations). This is the textbook "
        "oversold zone — pullbacks of this depth are rare in normal "
        "regimes. Mean-reversion strategies (RSI(2), Largecap Rebound) "
        "fire most reliably here.",
    ),
    "lower_band": (
        "Near lower band",
        "ok",
        "%B between 0 and 0.2 means today is trading near the lower "
        "band — moderate oversold. Setup zone for mean-reversion "
        "strategies; momentum strategies often skip these.",
    ),
    "lower_half": (
        "Lower half",
        "neutral",
        "%B between 0.2 and 0.5 — below the 20-day SMA but not at "
        "extremes. Normal pullback territory.",
    ),
    "upper_half": (
        "Upper half",
        "neutral",
        "%B between 0.5 and 0.8 — above the 20-day SMA, in normal "
        "uptrend territory. Trend strategies favor these readings.",
    ),
    "upper_band": (
        "Near upper band",
        "warn",
        "%B between 0.8 and 1.0 means today is trading near the upper "
        "band — moderately overbought. Trend strategies still active "
        "but new entries face higher reversion risk.",
    ),
    "above_upper": (
        "Above upper band",
        "warn",
        "%B above 1.0 means today's close pierced the upper band (the "
        "20-day SMA plus 2 standard deviations). Strongly overbought; "
        "in trending markets this is a genuine momentum signal, but in "
        "range-bound markets it's a high-probability fade. Read alongside "
        "the ADX value to choose your interpretation.",
    ),
}


def _pct_b_bucket(value: float) -> str:
    if value < 0:
        return "below_lower"
    if value < 0.2:
        return "lower_band"
    if value < 0.5:
        return "lower_half"
    if value < 0.8:
        return "upper_half"
    if value <= 1.0:
        return "upper_band"
    return "above_upper"


# ---------------------------------------------------------------------------
# BB width percentile buckets
# ---------------------------------------------------------------------------

_WIDTH_BUCKETS: dict[str, tuple[str, str, str]] = {
    "compressed": (
        "Severely compressed",
        "ok",  # opportunity color
        "BB width is in the bottom 10% of its trailing 6-month range. "
        "Volatility is severely compressed — Crabel's 'coiled spring' "
        "setup. Expansion historically follows within days, but the "
        "direction of the expansion is NOT signaled by the compression "
        "itself. Watch for the breakout direction; size into it.",
    ),
    "contracted": (
        "Contracted",
        "ok",
        "BB width is in the bottom 25% of its 6-month range. Volatility "
        "is contracting; a regime change is more likely than continuation. "
        "Setup window for breakout strategies.",
    ),
    "normal": (
        "Normal",
        "neutral",
        "BB width is in the middle 50% of its 6-month range. Volatility "
        "is in the typical range — no special setup, just normal "
        "trading conditions.",
    ),
    "expanded": (
        "Expanded",
        "warn",
        "BB width is in the top 25% of its 6-month range. Volatility "
        "is elevated — the move is mature. Mean reversion (rather than "
        "continuation) is more likely from these levels.",
    ),
    "extreme": (
        "At extremes",
        "bad",
        "BB width is in the top 10% of its 6-month range. Volatility "
        "is at extremes; trends rarely sustain through these readings. "
        "Tighten trailing stops on trend positions; favor mean-reversion "
        "setups for new entries.",
    ),
}


def _width_bucket(percentile: float) -> str:
    if percentile < 10:
        return "compressed"
    if percentile < 25:
        return "contracted"
    if percentile < 75:
        return "normal"
    if percentile < 90:
        return "expanded"
    return "extreme"


# ---------------------------------------------------------------------------
# State + computation
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BollingerState:
    available: bool

    # %B reading
    pct_b_value: float | None
    pct_b_bucket: str
    pct_b_label: str
    pct_b_kind: str
    pct_b_explanation: str

    # BB width percentile reading (0-100)
    width_value: float | None  # raw width (upper-lower)/middle, in fraction
    width_percentile: float | None  # 0-100
    width_bucket: str
    width_label: str
    width_kind: str
    width_explanation: str

    @classmethod
    def unavailable(cls) -> BollingerState:
        return cls(
            available=False,
            pct_b_value=None,
            pct_b_bucket="?",
            pct_b_label="n/a",
            pct_b_kind="neutral",
            pct_b_explanation="Insufficient SPY history to compute Bollinger %B.",
            width_value=None,
            width_percentile=None,
            width_bucket="?",
            width_label="n/a",
            width_kind="neutral",
            width_explanation="Insufficient SPY history to compute BB width percentile.",
        )


def compute_bollinger_state(
    spy_bars: pd.DataFrame | None,
    as_of: _date,
) -> BollingerState:
    """Compute %B + BB-width percentile from SPY's close history."""
    if spy_bars is None or spy_bars.empty or "close" not in spy_bars.columns:
        return BollingerState.unavailable()

    close = spy_bars["close"]
    bands = bollinger_bands(close, period=20, stddev=2.0)
    if bands.empty:
        return BollingerState.unavailable()

    upper = bands["upper"]
    lower = bands["lower"]
    middle = bands["middle"]

    last_close = float(close.iloc[-1])
    last_upper = upper.iloc[-1]
    last_lower = lower.iloc[-1]
    last_middle = middle.iloc[-1]

    if any(_is_nan(v) for v in (last_upper, last_lower, last_middle)):
        return BollingerState.unavailable()
    band_range = float(last_upper) - float(last_lower)
    if band_range <= 0:
        return BollingerState.unavailable()

    # %B = (close - lower) / (upper - lower).
    pct_b = (last_close - float(last_lower)) / band_range
    pct_b_bucket_key = _pct_b_bucket(pct_b)
    pb_label, pb_kind, pb_expl = _PCT_B_BUCKETS[pct_b_bucket_key]

    # BB width = (upper - lower) / middle. Compute the full series so
    # we can rank today against the trailing 6-month distribution.
    width_series = (upper - lower) / middle
    width_series = width_series.dropna()
    if len(width_series) < 30:  # need at least a month of history
        return BollingerState.unavailable()
    today_width = float(width_series.iloc[-1])

    lookback = width_series.iloc[-_BB_WIDTH_LOOKBACK:]
    # If we don't have a full 6 months yet, just rank against what we have.
    rank = (lookback <= today_width).sum()
    percentile = float(rank) / float(len(lookback)) * 100.0
    width_bucket_key = _width_bucket(percentile)
    w_label, w_kind, w_expl = _WIDTH_BUCKETS[width_bucket_key]

    return BollingerState(
        available=True,
        pct_b_value=pct_b,
        pct_b_bucket=pct_b_bucket_key,
        pct_b_label=pb_label,
        pct_b_kind=pb_kind,
        pct_b_explanation=pb_expl,
        width_value=today_width,
        width_percentile=percentile,
        width_bucket=width_bucket_key,
        width_label=w_label,
        width_kind=w_kind,
        width_explanation=w_expl,
    )


def _is_nan(v: object) -> bool:
    try:
        f = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return True
    return f != f
