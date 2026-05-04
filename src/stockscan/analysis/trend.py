"""Multi-timeframe trend classification for the symbol-level analysis.

Combines four signals into one bucket:

  1. **MA stack alignment** - does close > SMA(20) > SMA(50) > SMA(200)?
     A fully-aligned bullish stack is a strong bullish trend; the
     reverse is a strong bearish trend. Mixed alignment is neutral.

  2. **ADX(14)** - trend strength. Direction-blind so we use it as a
     gate ("is there a real trend at all?") rather than a primary
     direction signal.

  3. **Recent returns** - 5/21/63-day price changes. A consistent
     positive bias across all three is a strong bullish read; flip
     for bearish.

  4. **Distance from key MAs** - how far above/below SMA(20/50/200)
     is the close, in percent. Used for both the bucket and to feed
     options-context strike-selection hints.

Output bucket:
  * 'strong_up'   - fully aligned bullish stack, ADX > 25, all returns > 0
  * 'up'          - bullish stack OR positive returns + ADX present
  * 'neutral'     - mixed signals, no clear direction
  * 'down'        - bearish stack OR negative returns
  * 'strong_down' - fully aligned bearish, ADX > 25, all returns < 0
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from stockscan.analysis.state import TrendState
from stockscan.indicators import adx, sma

if TYPE_CHECKING:
    import pandas as pd


# Bucket descriptions for the dashboard chip + flavor text.
_TREND_BUCKETS: dict[str, tuple[str, str]] = {
    "strong_up": (
        "Strong uptrend",
        "Bullish MA stack (close > SMA20 > SMA50 > SMA200), ADX confirms "
        "a real trend, and recent returns are uniformly positive. Long-bias "
        "options strategies (call spreads, cash-secured short puts) are the "
        "natural fit; avoid fading these.",
    ),
    "up": (
        "Uptrend",
        "Bullish bias on either the MA stack or recent returns, but not "
        "both fully aligned. Long delta is favored but consider tighter "
        "strike selection - pullbacks to support are still likely.",
    ),
    "neutral": (
        "Neutral / chop",
        "Mixed signals across MA alignment, ADX, and recent returns. "
        "Direction is unclear; iron condors and other range-bound option "
        "strategies fit this regime better than directional plays.",
    ),
    "down": (
        "Downtrend",
        "Bearish bias on either the MA stack or recent returns. Short delta "
        "is favored but be careful with strike selection - bear bounces "
        "to resistance are common in downtrends.",
    ),
    "strong_down": (
        "Strong downtrend",
        "Bearish MA stack (close < SMA20 < SMA50 < SMA200), ADX confirms a "
        "real trend, and returns are uniformly negative. Long-puts or put "
        "spreads are the natural fit; don't fade by selling premium on "
        "the upside.",
    ),
}


def compute_trend(bars: pd.DataFrame) -> TrendState:
    """Run trend classification on a daily-bars DataFrame."""
    if bars is None or bars.empty or "close" not in bars.columns:
        return TrendState.unavailable()
    close = bars["close"]
    if len(close) < 200:
        return TrendState.unavailable()

    last_close = float(close.iloc[-1])
    if last_close <= 0:
        return TrendState.unavailable()

    # ---- Multi-timeframe MAs ----
    sma_20 = _safe_last(sma(close, 20))
    sma_50 = _safe_last(sma(close, 50))
    sma_200 = _safe_last(sma(close, 200))

    def pct_above(ma: float | None) -> float | None:
        if ma is None or ma <= 0:
            return None
        return (last_close - ma) / ma * 100

    p_20 = pct_above(sma_20)
    p_50 = pct_above(sma_50)
    p_200 = pct_above(sma_200)

    # ---- Stack alignment ----
    if (sma_20 and sma_50 and sma_200
            and last_close > sma_20 > sma_50 > sma_200):
        alignment = "aligned_bullish"
    elif (sma_20 and sma_50 and sma_200
            and last_close < sma_20 < sma_50 < sma_200):
        alignment = "aligned_bearish"
    else:
        alignment = "mixed"

    # ---- Recent returns ----
    def _pct_return(window: int) -> float | None:
        if len(close) <= window:
            return None
        prev = float(close.iloc[-1 - window])
        if prev <= 0:
            return None
        return (last_close / prev - 1.0) * 100

    r5 = _pct_return(5)
    r21 = _pct_return(21)
    r63 = _pct_return(63)

    # ---- ADX(14) ----
    adx_v: float | None = None
    if "high" in bars.columns and "low" in bars.columns and len(close) >= 30:
        last_adx = _safe_last(adx(bars["high"], bars["low"], close, 14))
        if last_adx is not None:
            adx_v = float(last_adx)

    # ---- Bucket ----
    bucket = _classify(alignment, adx_v, [r5, r21, r63])
    label, explanation = _TREND_BUCKETS.get(bucket, ("?", ""))

    return TrendState(
        available=True,
        bucket=bucket,
        label=label,
        explanation=explanation,
        return_5d=round(r5, 4) if r5 is not None else None,
        return_21d=round(r21, 4) if r21 is not None else None,
        return_63d=round(r63, 4) if r63 is not None else None,
        ma_alignment=alignment,
        sma_20=round(sma_20, 4) if sma_20 is not None else None,
        sma_50=round(sma_50, 4) if sma_50 is not None else None,
        sma_200=round(sma_200, 4) if sma_200 is not None else None,
        adx_14=round(adx_v, 4) if adx_v is not None else None,
        pct_above_sma20=round(p_20, 4) if p_20 is not None else None,
        pct_above_sma50=round(p_50, 4) if p_50 is not None else None,
        pct_above_sma200=round(p_200, 4) if p_200 is not None else None,
    )


def _classify(
    alignment: str,
    adx_v: float | None,
    returns: list[float | None],
) -> str:
    """Combine alignment + ADX + returns into one bucket label."""
    has_strong_adx = adx_v is not None and adx_v >= 25
    valid_returns = [r for r in returns if r is not None]
    if not valid_returns:
        return "neutral"
    all_pos = all(r > 0 for r in valid_returns)
    all_neg = all(r < 0 for r in valid_returns)
    avg_ret = sum(valid_returns) / len(valid_returns)

    if alignment == "aligned_bullish" and has_strong_adx and all_pos:
        return "strong_up"
    if alignment == "aligned_bearish" and has_strong_adx and all_neg:
        return "strong_down"
    if alignment == "aligned_bullish" or (all_pos and avg_ret > 1):
        return "up"
    if alignment == "aligned_bearish" or (all_neg and avg_ret < -1):
        return "down"
    return "neutral"


def _safe_last(series) -> float | None:
    """Return float of the last value, or None if NaN / missing."""
    if series is None or len(series) == 0:
        return None
    val = series.iloc[-1]
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return f
