"""Pivot proximity primitive — at a support/resistance level (signal-scoring spec §4.4).

A reversal is only worth trading at a level: positive near **support below**
(bottom location), negative near **resistance above** (top location). Distances
are in ATR units so "near" auto-scales to each stock's volatility.

"Near" is measured over the last ``prox_window`` bars (default 3), not just
today's close: a reversal confirms by hooking off the extreme, which lifts the
close away from the level it just tested, so a single-bar proximity check and the
turn signal peak on different days. Using the window's low/high keeps "at the
level" true for the 1-2 bars the turn takes to print.

No look-ahead: a swing pivot is only *confirmed* ``k`` bars after it prints (it
needs ``k`` bars on its right shoulder), so only pivots at index ``i <= as_of − k``
are considered — the load-bearing correctness point for this primitive. The
proximity window only reads bars ``<= as_of`` and does not change which pivots are
eligible.

Pure function: bars-only, no DB. Strategy-agnostic.
"""

from __future__ import annotations

import pandas as pd

from stockscan.indicators.ta import atr as compute_atr


def _clip(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def pivot_proximity(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    *,
    k: int = 3,
    lookback: int = 60,
    prox_atr: float = 1.5,
    atr_period: int = 14,
    prox_window: int = 3,
) -> dict[str, float] | None:
    """Signed proximity-to-level read in ``raw`` (+ near support / − near resistance).

    Returns None if there is insufficient history or ATR is unavailable.
    """
    n = len(close)
    if n < lookback + k + 1:
        return None
    atr_series = compute_atr(high, low, close, atr_period)
    atr_v = atr_series.iloc[-1]
    if pd.isna(atr_v) or float(atr_v) <= 0:
        return None
    atr_v = float(atr_v)
    last = float(close.iloc[-1])

    lows = low.to_numpy(dtype=float)
    highs = high.to_numpy(dtype=float)
    # Confirmed pivots: index i needs k bars on each side (i <= n-1-k = no
    # look-ahead) and must fall within the trailing lookback window.
    start = max(k, n - 1 - lookback)
    end = n - 1 - k
    support: float | None = None
    resistance: float | None = None
    for i in range(start, end + 1):
        lo_i = lows[i]
        hi_i = highs[i]
        if lo_i == lows[i - k : i + k + 1].min() and lo_i <= last:
            if support is None or lo_i > support:  # nearest support BELOW price
                support = lo_i
        if hi_i == highs[i - k : i + k + 1].max() and hi_i >= last:
            if resistance is None or hi_i < resistance:  # nearest resistance ABOVE
                resistance = hi_i

    # Proximity is measured against the closest the last `prox_window` bars came
    # to each level — not just today's close. A reversal *confirms* by hooking
    # off the extreme (reversal_trigger needs the up/down bar), which mechanically
    # lifts the close away from the level it just tested; without a window the
    # turn and the level peak on different days and neither alone clears the entry
    # bar. Using the window's low/high keeps "at the level" true for the 1-2 bars
    # it takes the turn to print. No look-ahead: these are all bars <= as_of, and
    # which pivots are *eligible* is unchanged (still confirmed-only).
    w = max(1, prox_window)
    approach_low = float(low.iloc[-w:].min())
    approach_high = float(high.iloc[-w:].max())

    near_sup = (
        _clip(1.0 - (approach_low - support) / (prox_atr * atr_v), 0.0, 1.0)
        if support is not None
        else 0.0
    )
    near_res = (
        _clip(1.0 - (resistance - approach_high) / (prox_atr * atr_v), 0.0, 1.0)
        if resistance is not None
        else 0.0
    )

    out: dict[str, float] = {
        "near_sup": round(near_sup, 4),
        "near_res": round(near_res, 4),
        "raw": _clip(near_sup - near_res),
    }
    if support is not None:
        out["support"] = round(support, 4)
        out["dist_sup_atr"] = round((approach_low - support) / atr_v, 4)
    if resistance is not None:
        out["resistance"] = round(resistance, 4)
        out["dist_res_atr"] = round((resistance - approach_high) / atr_v, 4)
    return out
