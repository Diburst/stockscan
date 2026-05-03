"""SPY ADX(14) state with bucketed interpretation.

ADX (Average Directional Index, Wilder 1978) is a 0-100 oscillator
measuring TREND STRENGTH — not direction. The same value of, say,
35 can correspond to a strong uptrend or a strong downtrend; ADX
itself is direction-blind.

The buckets below are the practitioner-consensus thresholds:

  * < 18  → range-bound / choppy. No directional move; trend-following
            strategies whipsaw here.
  * 18-25 → transitioning / ambiguous. A trend may be developing but
            isn't yet confirmed.
  * 25-40 → genuine directional trend in place. The sweet spot for
            breakout / momentum strategies.
  * 40-60 → very strong trend. Historically these readings precede
            consolidation rather than continuation; chasing entries
            here usually means buying the top.
  * > 60  → extreme reading. Rare; typically marks the late stage of
            an exhausted trend.

The bucket determines both the kind label (for the badge color) and
the curated flavor text shown beneath the value.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from stockscan.indicators import adx

if TYPE_CHECKING:
    from datetime import date as _date

    import pandas as pd


@dataclass(frozen=True, slots=True)
class AdxState:
    available: bool
    value: float | None
    bucket: str  # 'choppy' | 'transitioning' | 'trending' | 'strong_trend' | 'extreme' | '?'
    label: str  # human-readable label for the chip
    kind: str  # color bucket for the badge: 'ok' | 'warn' | 'bad' | 'neutral'
    explanation: str  # full flavor-text paragraph

    @classmethod
    def unavailable(cls) -> AdxState:
        return cls(
            available=False,
            value=None,
            bucket="?",
            label="n/a",
            kind="neutral",
            explanation=(
                "ADX(14) couldn't be computed (not enough SPY history)."
            ),
        )


# Curated bucket descriptions. Keep these factual + actionable rather
# than promotional; they appear under the number on the dashboard so
# the user reads them whenever the card is on screen.
_BUCKETS: dict[str, tuple[str, str, str]] = {
    "choppy": (
        "Choppy / range-bound",
        "warn",
        "ADX below 18 means there is no sustained directional move. "
        "Breakout and trend-following strategies (Donchian, 52-week-high) "
        "whipsaw in this regime; mean-reversion strategies (RSI(2)) tend "
        "to do best.",
    ),
    "transitioning": (
        "Transitioning",
        "neutral",
        "ADX between 18 and 25 is the ambiguous zone — a trend may be "
        "developing but it isn't confirmed yet. Wait for ADX to push above "
        "25 before sizing up trend strategies, or accept reduced position "
        "sizing in the meantime.",
    ),
    "trending": (
        "Trending",
        "ok",
        "ADX between 25 and 40 indicates a genuine directional trend is "
        "in place. This is the sweet spot for breakout and momentum "
        "strategies; mean-reversion strategies face headwinds in this "
        "regime as pullbacks tend to be shallower than expected.",
    ),
    "strong_trend": (
        "Strong trend",
        "warn",
        "ADX between 40 and 60 is a very strong trend. Counter-intuitively, "
        "these readings have historically PRECEDED consolidation rather "
        "than continuation — chasing late entries here often means buying "
        "the top of an exhausted move. Existing positions ride; new "
        "entries become higher-risk.",
    ),
    "extreme": (
        "Extreme",
        "bad",
        "ADX above 60 is a rare extreme reading — typically marks the "
        "late stage of an exhausted trend. Trend strategies should "
        "ignore new signals; consider tightening trailing stops on "
        "existing trend positions.",
    ),
}


def _bucket_for(value: float) -> str:
    if value < 18:
        return "choppy"
    if value < 25:
        return "transitioning"
    if value < 40:
        return "trending"
    if value < 60:
        return "strong_trend"
    return "extreme"


def compute_adx_state(
    spy_bars: pd.DataFrame | None,
    as_of: _date,
) -> AdxState:
    """Compute ADX(14) on SPY's high/low/close and bucket the result."""
    if spy_bars is None or spy_bars.empty:
        return AdxState.unavailable()
    needed = {"high", "low", "close"}
    if not needed.issubset(spy_bars.columns):
        return AdxState.unavailable()

    series = adx(spy_bars["high"], spy_bars["low"], spy_bars["close"], 14)
    if series.empty:
        return AdxState.unavailable()
    last = series.iloc[-1]
    # adx() uses Wilder smoothing — the early window is NaN until warmup
    # completes (~28 bars). If the latest reading is NaN we don't have
    # enough data; bail.
    if last is None:
        return AdxState.unavailable()
    try:
        value = float(last)
    except (TypeError, ValueError):
        return AdxState.unavailable()
    if value != value:  # NaN check (NaN != NaN)
        return AdxState.unavailable()

    bucket = _bucket_for(value)
    label, kind, explanation = _BUCKETS[bucket]
    return AdxState(
        available=True,
        value=value,
        bucket=bucket,
        label=label,
        kind=kind,
        explanation=explanation,
    )
