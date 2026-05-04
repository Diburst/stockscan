"""Support / resistance level detection.

Algorithm:

  1. **Pivot detection.** Walk the bar history with a configurable
     half-window (default 5). A bar is a pivot HIGH if its high is
     >= every other high in the surrounding ±N bars (excluding bars
     past today, which we don't know yet). Pivot LOW is symmetric
     on lows. This is the standard Larry Williams / Tom DeMark pivot
     definition.

  2. **Clustering.** Group nearby pivots together. Two pivots within
     ``cluster_tolerance_pct`` of each other (default 1.5%) are merged
     into a single level at their volume-weighted average price. This
     prevents a noisy double-top at $100.20 / $100.50 from being
     reported as two separate resistance levels.

  3. **Strength scoring.** Each cluster gets a [0, 1] score combining:
       - Touch count (more touches = more historically significant)
       - Recency (recent levels weight higher than year-old levels)
       - Pivot prominence (how much price reversed at the level)

  4. **Filter.** Drop clusters that are >25% from current price (too
     far away to matter for short-term options trading) and clusters
     with strength below a threshold.

The output is up to 4 support and 4 resistance levels, sorted by
strength descending.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from stockscan.analysis.state import Level

if TYPE_CHECKING:
    import pandas as pd


# Tunables - exposed as constants rather than function parameters so
# the engine layer doesn't have to thread them through.
_PIVOT_HALF_WINDOW = 5  # bars on each side
_CLUSTER_TOLERANCE_PCT = 1.5  # merge pivots within this % of each other
_MAX_DISTANCE_PCT = 25.0  # drop levels >25% from current price
_MIN_STRENGTH = 0.10  # drop very weak levels
_MAX_LEVELS_PER_SIDE = 4
# Weighting between touches/recency/prominence in the strength score.
_W_TOUCHES = 0.50
_W_RECENCY = 0.30
_W_PROMINENCE = 0.20


@dataclass(frozen=True, slots=True)
class _Pivot:
    """One detected pivot point - internal to clustering."""

    bar_index: int  # 0 = oldest, len-1 = most recent
    price: float
    kind: str  # 'high' | 'low'
    prominence: float  # absolute reversal magnitude


def find_support_resistance(
    bars: pd.DataFrame,
    *,
    half_window: int = _PIVOT_HALF_WINDOW,
    cluster_tolerance_pct: float = _CLUSTER_TOLERANCE_PCT,
) -> list[Level]:
    """Detect S/R levels from the bar history.

    Returns up to 4 support + 4 resistance levels, sorted by strength
    descending (highest strength first within each kind).
    """
    if bars is None or bars.empty:
        return []
    if not {"high", "low", "close"}.issubset(bars.columns):
        return []
    n = len(bars)
    if n < (2 * half_window + 1):
        return []

    last_close = float(bars["close"].iloc[-1])
    if last_close <= 0:
        return []

    pivots = _find_pivots(bars, half_window=half_window)
    if not pivots:
        return []

    # Each cluster is scored independently and then assigned a kind
    # ("support" | "resistance") based on its center price's position
    # relative to the current close — NOT based on which pivot type
    # produced it. The pivot origin is preserved separately on each
    # Level so flipped roles (broken-resistance-now-support, etc.) can
    # be flagged in the UI via Level.is_flipped.
    levels: list[Level] = []
    levels.extend(
        _cluster_and_score(
            [p for p in pivots if p.kind == "high"],
            origin="pivot_high",
            n_bars=n,
            last_close=last_close,
            cluster_tolerance_pct=cluster_tolerance_pct,
        )
    )
    levels.extend(
        _cluster_and_score(
            [p for p in pivots if p.kind == "low"],
            origin="pivot_low",
            n_bars=n,
            last_close=last_close,
            cluster_tolerance_pct=cluster_tolerance_pct,
        )
    )

    # Filter by distance + strength.
    levels = [
        lv for lv in levels
        if abs(lv.distance_pct) <= _MAX_DISTANCE_PCT and lv.strength >= _MIN_STRENGTH
    ]

    # Cap to top N per side.
    supports = sorted(
        [lv for lv in levels if lv.kind == "support"],
        key=lambda level: level.strength, reverse=True,
    )[:_MAX_LEVELS_PER_SIDE]
    resistances = sorted(
        [lv for lv in levels if lv.kind == "resistance"],
        key=lambda level: level.strength, reverse=True,
    )[:_MAX_LEVELS_PER_SIDE]

    return supports + resistances


# ---------------------------------------------------------------------------
# Pivot detection
# ---------------------------------------------------------------------------


def _find_pivots(bars: pd.DataFrame, *, half_window: int) -> list[_Pivot]:
    """Walk the bars and identify pivot highs and lows.

    A pivot HIGH at index ``i`` requires that ``high[i]`` is >= every
    other high in the window ``[i - half_window, i + half_window]``.
    The first and last ``half_window`` bars can't be pivots because
    the window extends past the data.
    """
    highs = bars["high"].to_numpy(dtype=float)
    lows = bars["low"].to_numpy(dtype=float)
    n = len(highs)
    pivots: list[_Pivot] = []
    for i in range(half_window, n - half_window):
        # Pivot high?
        window_h = highs[i - half_window : i + half_window + 1]
        if highs[i] >= window_h.max():
            # Prominence: how much price reversed after this pivot.
            # Use the lowest LOW in the window after i as the floor.
            tail_low = float(lows[i : i + half_window + 1].min())
            prom = float(highs[i]) - tail_low
            pivots.append(
                _Pivot(bar_index=i, price=float(highs[i]),
                       kind="high", prominence=prom)
            )
        # Pivot low?
        window_l = lows[i - half_window : i + half_window + 1]
        if lows[i] <= window_l.min():
            tail_high = float(highs[i : i + half_window + 1].max())
            prom = tail_high - float(lows[i])
            pivots.append(
                _Pivot(bar_index=i, price=float(lows[i]),
                       kind="low", prominence=prom)
            )
    return pivots


# ---------------------------------------------------------------------------
# Cluster + score
# ---------------------------------------------------------------------------


def _cluster_and_score(
    pivots: list[_Pivot],
    *,
    origin: str,
    n_bars: int,
    last_close: float,
    cluster_tolerance_pct: float,
) -> list[Level]:
    """Single-pass agglomerative clustering of pivots by price proximity.

    Pivots are sorted by price; consecutive pivots within
    ``cluster_tolerance_pct`` of each other merge into one cluster.
    Each cluster's center price is the average of its members, and its
    strength score blends touches, recency, and prominence.

    The cluster's ``kind`` ("support" | "resistance") is determined per
    level from where its center sits relative to ``last_close``: a
    pivot-high cluster that price has since broken out above is now a
    *support* level. The original pivot type is preserved in ``origin``
    for the UI to flag the role-reversal.
    """
    if not pivots:
        return []
    sorted_pivots = sorted(pivots, key=lambda p: p.price)
    clusters: list[list[_Pivot]] = []
    current: list[_Pivot] = [sorted_pivots[0]]
    for p in sorted_pivots[1:]:
        center = sum(q.price for q in current) / len(current)
        if abs(p.price - center) / max(center, 1e-9) * 100 <= cluster_tolerance_pct:
            current.append(p)
        else:
            clusters.append(current)
            current = [p]
    clusters.append(current)

    # Score each cluster.
    levels: list[Level] = []
    # For touch-count and prominence normalization, we want a baseline
    # so the scores live in roughly [0, 1].
    max_touches = max(len(c) for c in clusters)
    max_prom = max(
        (max((q.prominence for q in c), default=0.0) for c in clusters),
        default=0.0,
    ) or 1.0

    for cluster in clusters:
        center_price = sum(q.price for q in cluster) / len(cluster)
        touches = len(cluster)
        # Most-recent pivot in the cluster (highest bar index).
        most_recent_idx = max(q.bar_index for q in cluster)
        last_touch_days_ago = n_bars - 1 - most_recent_idx
        # Pivot prominence = max prominence among pivots in the cluster.
        prom = max(q.prominence for q in cluster)

        # Component sub-scores (each in [0, 1]).
        touch_score = min(1.0, touches / max(max_touches, 1))
        # Recency: 1.0 if last touch is today, decaying linearly to 0
        # at 252 trading days ago (one year).
        recency_score = max(0.0, 1.0 - (last_touch_days_ago / 252.0))
        prominence_score = min(1.0, prom / max_prom)

        strength = (
            _W_TOUCHES * touch_score
            + _W_RECENCY * recency_score
            + _W_PROMINENCE * prominence_score
        )

        distance_pct = (center_price - last_close) / last_close * 100
        # Polarity / role-reversal: kind is set from current price, not
        # from pivot origin. Tie (price == last_close, vanishingly rare
        # at 4dp rounding) breaks toward "resistance" for safety.
        kind = "resistance" if center_price >= last_close else "support"
        levels.append(
            Level(
                price=round(center_price, 4),
                kind=kind,
                strength=round(strength, 4),
                touches=touches,
                last_touch_days_ago=last_touch_days_ago,
                distance_pct=round(distance_pct, 4),
                origin=origin,
            )
        )

    return levels
