"""Reversal trigger primitive — exhaustion + the turn (signal-scoring spec §4.2).

Fires only when the fast oscillator (RSI(2), Connors' primitive) reached an
extreme **and is now hooking back** — being oversold is not enough; the turn must
show in the last 1-2 bars. Folds depth-of-extreme and the hook (+ a confirming
bar) into one signed value: positive = bottom turn, negative = top turn. A name
pinned deep-oversold but still falling scores ~0 (no hook yet) — the "don't catch
the knife mid-air" discipline.

Pure function: bars-only, no DB, no look-ahead. Strategy-agnostic — it returns
an intrinsic signed read; a strategy or composite decides how to weight it.
"""

from __future__ import annotations

import pandas as pd

from stockscan.indicators.ta import rsi as compute_rsi


def _clip(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def reversal_trigger(
    close: pd.Series,
    *,
    rsi_period: int = 2,
    os: float = 10.0,
    ob: float = 90.0,
) -> dict[str, float] | None:
    """Signed reversal-turn read in ``raw`` (+ bottom / − top), or None if < 15 bars.

    The returned dict carries ``rsi2``, ``rsi2_prev`` and ``raw`` (the signed,
    clipped value in [-1, +1]).
    """
    c = close.dropna()
    if len(c) < 15:
        return None
    rsi2 = compute_rsi(c, rsi_period)
    a, b = rsi2.iloc[-1], rsi2.iloc[-2]
    if pd.isna(a) or pd.isna(b):
        return None
    a, b = float(a), float(b)
    lo2, hi2 = min(a, b), max(a, b)
    last, prev = float(c.iloc[-1]), float(c.iloc[-2])

    # Bottom (bullish reversal): was oversold in the last 2 bars, now turning up.
    depth_b = _clip((os - lo2) / os, 0.0, 1.0)
    bull = depth_b * (0.5 * (a > b) + 0.5 * (last > prev)) if lo2 <= os else 0.0

    # Top (bearish reversal): mirror.
    depth_t = _clip((hi2 - ob) / (100.0 - ob), 0.0, 1.0)
    bear = depth_t * (0.5 * (a < b) + 0.5 * (last < prev)) if hi2 >= ob else 0.0

    return {
        "rsi2": round(a, 4),
        "rsi2_prev": round(b, 4),
        "raw": _clip(bull - bear),
    }
