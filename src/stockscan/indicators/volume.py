"""Volume confirmation primitive — Wyckoff multi-bar climax / absorption detector.

Wyckoff (and Tom Williams' VSA refinement) distinguish two reversal-confirming
volume patterns:

  - **Climax**: wide-spread bar with very high volume, close rejected the
    extreme it was driving toward (down day closing in the upper half of its
    range = bullish capitulation; up day closing in the lower half = bearish
    distribution). The "panic / euphoria" bar at the end of a one-way move.

  - **Absorption**: narrow-spread bar with very high volume, same direction
    rejection. Indicates institutional positioning soaking up supply (bullish)
    or demand (bearish) at a level — quieter on the chart than a climax but
    just as load-bearing for the reversal.

The single-bar v1 version of this function only looked at the entry bar; in
practice the climax usually prints 1-3 bars BEFORE the hook that gates entry,
so the climax was being missed and the multiplier floored at ``vol_floor`` on
nearly every TSLA trade in bt20–bt25. The v2 implementation scans the last
``lookback_bars`` bars, identifies the strongest climax/absorption candidate
in either direction, and returns its score as the multiplier base.

Returned multiplier is in ``[vol_floor, 1.0]``: it can only scale a signed
directional read, never flip its sign (volume carries no direction of its own).
Strategy/composite that consumes the multiplier knows the direction.

Pure function: bars-only, no DB.
"""

from __future__ import annotations

import pandas as pd


def _clip(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def volume_confirm(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    volume: pd.Series,
    *,
    rvol_window: int = 50,
    spike_mult: float = 1.5,
    vol_floor: float = 0.75,
    lookback_bars: int = 5,
    wide_spread_ratio: float = 1.3,
    narrow_spread_ratio: float = 0.7,
) -> dict[str, float] | None:
    """Wyckoff multi-bar climax/absorption detector → conviction multiplier.

    Parameters
    ----------
    rvol_window
        Lookback for the median-volume baseline (excludes the scan window).
    spike_mult
        A bar's ``rvol = volume / median_volume`` must exceed this to be a
        climax candidate; saturates the spike score at ``2 × spike_mult``.
    vol_floor
        Minimum multiplier returned. ``[vol_floor, 1.0]`` is the output range.
    lookback_bars
        How many recent bars to scan for the strongest climax candidate. The
        canonical Wyckoff "climax + automatic rally" pattern develops over
        2-5 bars; 5 is a reasonable default.
    wide_spread_ratio
        A bar's spread (high − low) must exceed this multiple of the median
        spread to qualify as a *climax* pattern (wide-spread capitulation).
    narrow_spread_ratio
        A bar's spread must be below this multiple of the median spread to
        qualify as an *absorption* pattern (narrow-spread institutional
        positioning).

    Returns
    -------
    dict with:
      - ``multiplier``: in ``[vol_floor, 1.0]``, the conviction scaler
      - ``rvol_med``: rvol of the best candidate bar (vs. baseline median)
      - ``clv``: close-location value [-1, +1] of the best candidate bar
      - ``spread_ratio``: spread of the best candidate vs. baseline median
      - ``climax_offset``: index offset (e.g. -1 = today, -2 = yesterday) of
        the best candidate. Useful for "climax detected 2 bars ago" UI hints.
      - ``climax_kind``: ``"climax"`` (wide spread), ``"absorption"`` (narrow),
        ``"mixed"`` (normal spread), or ``"none"`` (no candidate found).
      - ``climax_direction``: ``"bullish"``, ``"bearish"``, or ``"none"``.
      - ``reject``: backward-compat alias of the best candidate's climax
        score (the rejection-strength signal).

    Returns ``None`` on insufficient history.
    """
    n = len(close)
    if n < rvol_window + lookback_bars + 1:
        return None

    # Baselines: use bars *before* the scan window so a climax bar doesn't
    # poison its own baseline. This is also the no-look-ahead behaviour the
    # primitive enforces — every value used is at index < (n - lookback_bars).
    baseline_end = n - lookback_bars
    baseline_vol = volume.iloc[baseline_end - rvol_window : baseline_end]
    baseline_spread = (high - low).iloc[baseline_end - rvol_window : baseline_end]

    vol_med = float(baseline_vol.median())
    spread_med = float(baseline_spread.median())
    if pd.isna(vol_med) or pd.isna(spread_med) or vol_med <= 0 or spread_med <= 0:
        return None

    # Track the strongest climax candidate across the scan window.
    best = {
        "score": 0.0,
        "rvol": 0.0,
        "clv": 0.0,
        "spread_ratio": 0.0,
        "offset": 0,
        "kind": "none",
        "direction": "none",
    }

    for i in range(lookback_bars):
        offset = -1 - i  # -1 = last bar, -2 = bar before, ...
        if abs(offset) > n - 1:
            break

        h_i = float(high.iloc[offset])
        l_i = float(low.iloc[offset])
        cl_i = float(close.iloc[offset])
        v_i = float(volume.iloc[offset])
        prev_cl = float(close.iloc[offset - 1])

        rng = h_i - l_i
        if rng <= 0:
            continue

        rvol = v_i / vol_med
        # Volume below baseline can't be a climax — short-circuit early.
        if rvol < 1.0:
            continue

        # Saturation: bar with rvol = spike_mult scores 0; rvol = 2·spike_mult
        # saturates at 1.0. Below spike_mult contributes nothing.
        spike_s = _clip((rvol - spike_mult) / spike_mult)
        if spike_s <= 0:
            continue

        # Close-location value: where in the bar's range did we close?
        # +1 = at the high, −1 = at the low.
        clv = ((cl_i - l_i) - (h_i - cl_i)) / rng

        # Spread classification: wide (climax), narrow (absorption), or normal
        # (mixed — counts but at reduced weight). Both Wyckoff patterns are
        # reversal-confirming; the strategy doesn't care which kind, just that
        # one was present.
        spread_ratio = rng / spread_med
        if spread_ratio >= wide_spread_ratio:
            kind = "climax"
            spread_factor = 1.0  # full strength
        elif spread_ratio <= narrow_spread_ratio:
            kind = "absorption"
            spread_factor = 0.85  # very strong but slightly under climax
        else:
            kind = "mixed"
            spread_factor = 0.5   # normal-spread bar with high vol + rejection
                                  # still indicates something, but weaker

        # Direction inference + rejection check.
        #   Down bar (cl < prev) closing in upper half  → bullish rejection
        #   Up bar  (cl > prev) closing in lower half   → bearish rejection
        # Anything else (down bar closing low, up bar closing high) is a
        # continuation pattern, not a climax — skip.
        if cl_i < prev_cl and clv > 0:
            score = spike_s * clv * spread_factor
            direction = "bullish"
        elif cl_i > prev_cl and clv < 0:
            score = spike_s * (-clv) * spread_factor
            direction = "bearish"
        else:
            continue

        if score > best["score"]:
            best = {
                "score": score,
                "rvol": rvol,
                "clv": clv,
                "spread_ratio": spread_ratio,
                "offset": offset,
                "kind": kind,
                "direction": direction,
            }

    multiplier = vol_floor + (1.0 - vol_floor) * _clip(best["score"])

    return {
        "multiplier": round(multiplier, 4),
        "rvol_med": round(best["rvol"], 4),
        "clv": round(best["clv"], 4),
        "spread_ratio": round(best["spread_ratio"], 4),
        "climax_offset": best["offset"],
        "climax_kind": best["kind"],
        "climax_direction": best["direction"],
        # Backward-compat: the old single-bar version returned "reject" which
        # the signal-detail template already renders. Map to the climax score.
        "reject": round(best["score"], 4),
    }
