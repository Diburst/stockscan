"""Fibonacci retracement levels.

Given a bar history and a lookback window, this primitive picks two
anchors — the highest high and the lowest low in the window — and emits
the five canonical retracement ratios between them: 23.6%, 38.2%, 50.0%,
61.8%, 78.6%. The direction (retracing down from a recent high vs. up
from a recent low) is inferred from which anchor is more recent.

This is the most automatable subset of Fibonacci analysis. We deliberately
skip the extension levels (127.2, 161.8, etc.) and the user-anchor case
(traders manually picking arbitrary swings) — both add UI surface area
without clearly improving the typical short-term-options decision the
Analysis page is built around.

Conventional formulas (Fibonacci-ratio family — Edwards & Magee; Brown,
"Fibonacci Analysis"):

    range = high - low
    level(r) = high - r * range        (works for both directions —
                                        same numerical price, different
                                        interpretation)

A "down from high" anchor pair means current price is somewhere below the
swing high; the retracement levels act as candidate SUPPORT during the
pullback. "up from low" inverts: the levels are candidate RESISTANCE on
the bounce. The caller decides how to render based on `direction`.
"""

from __future__ import annotations

from datetime import date as _date
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pandas as pd


# Default lookback for anchor selection: 120 trading days (~6 months).
# Covers the typical "swing" horizon — long enough to encompass a real
# trend leg, short enough that the anchor pair stays relevant for current-
# price decisions.
DEFAULT_LOOKBACK_BARS = 120

# The five ratios shown by every major charting platform. We drop 0% and
# 100% from the level list because those are just the anchor prices
# themselves — they're useful for the chart's range readout but redundant
# as "potential S/R" lines.
FIB_RATIOS: tuple[float, ...] = (0.236, 0.382, 0.500, 0.618, 0.786)


def fibonacci_retracement(
    bars: pd.DataFrame,
    *,
    lookback: int = DEFAULT_LOOKBACK_BARS,
) -> dict[str, Any] | None:
    """Find anchor high + anchor low in the trailing lookback and emit
    the canonical retracement ratios between them.

    Returns ``None`` when the input has fewer than ``lookback`` bars or
    the anchor high equals the anchor low (a flat market — no swing to
    retrace).

    Output::

        {
          "high":      float,            # anchor high price
          "low":       float,            # anchor low price
          "high_date": date,             # bar date of the anchor high
          "low_date":  date,             # bar date of the anchor low
          "direction": "down_from_high"  # high is more recent → retracing
                       | "up_from_low",  #   down; vice versa for "up"
          "levels": [                    # 5 entries, top-down by price
            {"ratio": 0.236, "price": ..., "label": "23.6%"},
            {"ratio": 0.382, "price": ..., "label": "38.2%"},
            {"ratio": 0.500, "price": ..., "label": "50.0%"},
            {"ratio": 0.618, "price": ..., "label": "61.8%"},
            {"ratio": 0.786, "price": ..., "label": "78.6%"},
          ],
        }
    """
    if bars is None or bars.empty:
        return None
    if not {"high", "low"}.issubset(bars.columns):
        return None
    if len(bars) < lookback:
        return None

    window = bars.iloc[-lookback:]
    high_value = float(window["high"].max())
    low_value = float(window["low"].min())
    if high_value <= low_value:
        return None  # flat window — no swing to anchor on

    # The argmax / argmin on the trimmed window returns a positional index
    # 0..lookback-1; map back to the bar's date via the DatetimeIndex.
    high_pos = int(window["high"].to_numpy().argmax())
    low_pos = int(window["low"].to_numpy().argmin())
    high_ts = window.index[high_pos]
    low_ts = window.index[low_pos]
    high_date = high_ts.date() if hasattr(high_ts, "date") else _date.today()
    low_date = low_ts.date() if hasattr(low_ts, "date") else _date.today()

    # Direction: which anchor is more recent? In a fresh uptrend the high
    # is the latest bar; in a fresh downtrend the low is. A tie (both on
    # the same bar — impossible in practice for high ≠ low) breaks toward
    # "down_from_high" for safety.
    direction = "down_from_high" if high_pos >= low_pos else "up_from_low"

    rng = high_value - low_value
    levels = [
        {
            "ratio": r,
            "price": round(high_value - r * rng, 4),
            "label": f"{r * 100:.1f}%",
        }
        for r in FIB_RATIOS
    ]

    return {
        "high": round(high_value, 4),
        "low": round(low_value, 4),
        "high_date": high_date,
        "low_date": low_date,
        "direction": direction,
        "levels": levels,
    }
