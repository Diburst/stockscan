"""RSI(14) + MACD readings with bucketed labels.

Both indicators are widely used in options trading for entry timing.
RSI gives an instantaneous overbought/oversold read; MACD gives a
medium-term momentum + crossover signal.

We don't try to detect divergences here - that's a v2 feature when
we want pattern recognition. For the engine's purpose, the raw
values + bucket labels are enough for the dashboard.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from stockscan.analysis.state import MomentumState
from stockscan.indicators import macd, rsi

if TYPE_CHECKING:
    import pandas as pd


# RSI bucket boundaries - the conventional Wilder thresholds.
def _rsi_bucket(value: float) -> tuple[str, str]:
    """Return (bucket_key, human_label)."""
    if value <= 30:
        return "oversold", "Oversold"
    if value <= 40:
        return "low", "Low / weak"
    if value <= 60:
        return "neutral", "Neutral"
    if value <= 70:
        return "high", "High / strong"
    return "overbought", "Overbought"


# MACD state buckets - based on the histogram (signed magnitude of
# MACD line minus signal line) and whether a crossover just happened.
def _macd_state(
    line: float, signal: float, histogram: float,
    prev_histogram: float | None,
) -> tuple[str, str]:
    """Classify MACD state. ``prev_histogram`` lets us detect crosses."""
    if prev_histogram is not None:
        # Bullish cross: histogram flipped from negative to positive.
        if prev_histogram <= 0 and histogram > 0:
            return "bullish_cross", "Bullish cross (just turned up)"
        # Bearish cross: histogram flipped from positive to negative.
        if prev_histogram >= 0 and histogram < 0:
            return "bearish_cross", "Bearish cross (just turned down)"
    if histogram > 0:
        return "bullish", "Bullish (histogram positive)"
    if histogram < 0:
        return "bearish", "Bearish (histogram negative)"
    return "neutral", "Neutral (histogram at zero)"


def compute_momentum(bars: pd.DataFrame) -> MomentumState:
    """Run RSI + MACD on a daily-bars DataFrame."""
    if bars is None or bars.empty or "close" not in bars.columns:
        return MomentumState.unavailable()
    close = bars["close"]
    if len(close) < 35:
        return MomentumState.unavailable()

    # ---- RSI(14) ----
    rsi_v: float | None = None
    rsi_bucket = "?"
    rsi_label = "n/a"
    rsi_series = rsi(close, 14)
    if len(rsi_series):
        last = rsi_series.iloc[-1]
        try:
            rsi_v = float(last)
            if rsi_v != rsi_v:  # NaN
                rsi_v = None
        except (TypeError, ValueError):
            rsi_v = None
    if rsi_v is not None:
        rsi_bucket, rsi_label = _rsi_bucket(rsi_v)

    # ---- MACD ----
    macd_df = macd(close, fast=12, slow=26, signal=9)
    macd_line: float | None = None
    macd_signal: float | None = None
    macd_hist: float | None = None
    macd_state = "?"
    macd_label = "n/a"
    if not macd_df.empty and len(macd_df) >= 2:
        last_line = macd_df["macd"].iloc[-1]
        last_sig = macd_df["signal"].iloc[-1]
        last_hist = macd_df["histogram"].iloc[-1]
        prev_hist = macd_df["histogram"].iloc[-2]
        try:
            macd_line = float(last_line)
            macd_signal = float(last_sig)
            macd_hist = float(last_hist)
            prev_h: float | None = float(prev_hist)
        except (TypeError, ValueError):
            macd_line = macd_signal = macd_hist = None
            prev_h = None
        # NaN check
        for v in (macd_line, macd_signal, macd_hist):
            if v is not None and v != v:
                macd_line = macd_signal = macd_hist = None
                break
        if macd_line is not None and macd_hist is not None:
            macd_state, macd_label = _macd_state(
                macd_line, macd_signal or 0.0, macd_hist, prev_h,
            )

    # Compose a one-paragraph explanation that's mounted under the
    # readings on the detail page.
    explanation_parts: list[str] = []
    if rsi_v is not None:
        explanation_parts.append(
            f"RSI(14) at {rsi_v:.1f} ({rsi_label.lower()}). "
            f"{'Mean-reversion strategies favor oversold reads; momentum strategies prefer high readings.' if rsi_bucket in ('oversold', 'overbought') else 'In the neutral band - direction-agnostic.'}"
        )
    if macd_line is not None and macd_hist is not None:
        explanation_parts.append(
            f"MACD line {macd_line:+.3f}, histogram {macd_hist:+.3f}: {macd_label.lower()}."
        )
    explanation = " ".join(explanation_parts) or "Momentum readings unavailable."

    return MomentumState(
        available=rsi_v is not None or macd_line is not None,
        rsi_14=round(rsi_v, 4) if rsi_v is not None else None,
        rsi_bucket=rsi_bucket,
        rsi_label=rsi_label,
        macd_line=round(macd_line, 4) if macd_line is not None else None,
        macd_signal=round(macd_signal, 4) if macd_signal is not None else None,
        macd_histogram=round(macd_hist, 4) if macd_hist is not None else None,
        macd_state=macd_state,
        macd_label=macd_label,
        explanation=explanation,
    )
