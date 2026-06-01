"""Trend stack & slope primitive — intermediate-trend context (signal-scoring spec §4.1).

Absolute intermediate trend: where price sits vs its 50-day (primary) and 200-day
(lighter context) SMAs, plus the 50-day slope. Bands are calibrated for
single-stock dispersion, NOT the index regime values (porting those is a bug —
they saturate every name; see the spec calibration note).

This is a directional read; the *reinforce-only* policy (it may only add
conviction when it agrees with a core direction) belongs to whatever combines it,
not to the primitive. The function just returns the natural signed ``raw``.

Pure function: bars-only, no DB, no look-ahead.
"""

from __future__ import annotations

import pandas as pd


def _clip(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def trend_location(
    close: pd.Series,
    *,
    pos50_band: float = 0.10,
    pos200_band: float = 0.25,
    slope_band: float = 0.05,
    slope_window: int = 20,
) -> dict[str, float] | None:
    """Signed trend read in ``raw`` (in [-1, +1]). None if < 60 bars.

    Graceful degradation: with 60-219 bars the 200-day term is dropped and its
    weight renormalized onto pos50/slope (so new-ish listings still score).
    """
    c = close.dropna()
    if len(c) < 60:
        return None
    last = float(c.iloc[-1])

    sma50 = c.rolling(50).mean()
    sma50_last = sma50.iloc[-1]
    if pd.isna(sma50_last) or float(sma50_last) <= 0:
        return None
    sma50_last = float(sma50_last)
    pos50 = _clip((last - sma50_last) / sma50_last / pos50_band)

    slope_n = 0.0
    if len(sma50) > slope_window:
        prev = sma50.iloc[-1 - slope_window]
        if pd.notna(prev) and float(prev) > 0:
            slope_n = _clip(((sma50_last - float(prev)) / float(prev)) / slope_band)

    out: dict[str, float] = {"sma50": round(sma50_last, 4), "pos50": pos50, "slope_n": slope_n}

    if len(c) >= 220:
        sma200_last = c.rolling(200).mean().iloc[-1]
        if pd.notna(sma200_last) and float(sma200_last) > 0:
            pos200 = _clip((last - float(sma200_last)) / float(sma200_last) / pos200_band)
            out["pos200"] = pos200
            out["raw"] = _clip(0.40 * pos50 + 0.20 * pos200 + 0.40 * slope_n)
            return out

    # pos200 unavailable → renormalize 0.40/0.40 onto pos50/slope (→ 0.5/0.5).
    out["raw"] = _clip(0.5 * pos50 + 0.5 * slope_n)
    return out
