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
     into a single level at the arithmetic mean of their prices. This
     prevents a noisy double-top at $100.20 / $100.50 from being
     reported as two separate resistance levels.

  3. **Strength scoring.** Each cluster gets a [0, 1] score combining:
       - **Volume-weighted touches** — sum of bar volume across the
         pivots in the cluster, normalised to the heaviest cluster in
         the run. Per the discussion in the project history, raw touch
         count is the dominant single-symbol convention but maps poorly
         across symbols and treats a low-volume tag the same as a heavy
         institutional defence. Volume-weighting brings the score closer
         to professional Volume-Profile practice without inventing a new
         primitive.
       - Recency (linear decay; 1.0 today → 0.0 at 252 trading days).
       - Pivot prominence (max reversal magnitude in the cluster,
         normalised to the run max).

  4. **Weekly confirmation.** After daily clustering, the bar history
     is resampled to weekly OHLCV and pivots are found on that series
     with a smaller half-window (3 weeks ≈ 1.5 months around each
     pivot). Any daily cluster whose center sits within the cluster
     tolerance of a weekly pivot is marked ``confirmed_by_weekly`` and
     gets a 1.3× strength multiplier (capped at 1.0). Multi-timeframe
     confirmation is the long-standing tiebreak that separates
     structural levels from coincidence.

  5. **Filter.** Drop clusters that are >25% from current price (too
     far away to matter for short-term options trading) and clusters
     with strength below a threshold.

The output is up to 3 support and 3 resistance levels, sorted by
strength descending. The cap was tightened from 4 to 3 when the chart
gained Fibonacci retracements + implied-move bands as additional
horizontal-line sources — keeping pivot S/R to the top-3 most
historically defended levels prevents chart-line overload.
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
_MAX_LEVELS_PER_SIDE = 3  # tightened from 4 → 3 — see module docstring §5
# Weighting between volume-weighted touches / recency / prominence in the
# strength score. The numeric weights match the prior touches/recency/
# prominence split — only the semantic of "touches" changed (volume-
# weighted vs. count). Component sub-scores sum to 1.0.
_W_TOUCHES = 0.50
_W_RECENCY = 0.30
_W_PROMINENCE = 0.20

# Weekly-pivot confirmation tuning.
_WEEKLY_PIVOT_HALF_WINDOW = 3  # ~1.5 months around each weekly pivot
_WEEKLY_CONFIRM_MULTIPLIER = 1.3  # bump for daily ∩ weekly levels


@dataclass(frozen=True, slots=True)
class _Pivot:
    """One detected pivot point - internal to clustering."""

    bar_index: int  # 0 = oldest, len-1 = most recent
    price: float
    kind: str  # 'high' | 'low'
    prominence: float  # absolute reversal magnitude
    volume: float  # bar volume at the pivot (0.0 if not available)


def find_support_resistance(
    bars: pd.DataFrame,
    *,
    half_window: int = _PIVOT_HALF_WINDOW,
    cluster_tolerance_pct: float = _CLUSTER_TOLERANCE_PCT,
) -> list[Level]:
    """Detect S/R levels from the bar history.

    Returns up to 3 support + 3 resistance levels, sorted by strength
    descending (highest strength first within each kind). Levels confirmed
    by a weekly-resampled pivot carry ``confirmed_by_weekly=True`` and a
    1.3× strength multiplier (capped at 1.0).
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

    # Daily clusters are scored first; weekly confirmation comes after so
    # the bump can ride on top of the volume-weighted touch score.
    weekly_pivot_prices = _weekly_pivot_prices(bars)

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
            weekly_pivot_prices=weekly_pivot_prices,
        )
    )
    levels.extend(
        _cluster_and_score(
            [p for p in pivots if p.kind == "low"],
            origin="pivot_low",
            n_bars=n,
            last_close=last_close,
            cluster_tolerance_pct=cluster_tolerance_pct,
            weekly_pivot_prices=weekly_pivot_prices,
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
# Weekly resampling
# ---------------------------------------------------------------------------


def _weekly_pivot_prices(bars: pd.DataFrame) -> list[float]:
    """Resample daily bars to weekly OHLC and emit pivot prices.

    Used purely as a multi-timeframe confirmation signal — the daily
    scoring is already done; this just gives us a set of "price levels
    that also show up structurally on the weekly chart". Pivots are
    located with a smaller half-window (3 weeks ≈ 1.5 months on either
    side) than the daily pass — enough confirmation without requiring
    a quarter-long window before a level can qualify.

    Returns a flat list of pivot prices (both highs and lows) — the
    cluster matcher only cares about price coincidence, not kind.
    """
    if bars is None or bars.empty:
        return []
    if not hasattr(bars.index, "freq") and not hasattr(bars.index, "to_period"):
        return []
    if not {"high", "low"}.issubset(bars.columns):
        return []
    try:
        weekly = bars.resample("W").agg({
            "high": "max",
            "low": "min",
            # We don't actually use volume on weekly pivots, but resample
            # complains if columns are partially specified; including the
            # column keeps the frame shape predictable.
            "volume": "sum",
        }).dropna(subset=["high", "low"])
    except Exception:
        return []
    if len(weekly) < (2 * _WEEKLY_PIVOT_HALF_WINDOW + 1):
        return []

    highs = weekly["high"].to_numpy(dtype=float)
    lows = weekly["low"].to_numpy(dtype=float)
    n = len(highs)
    prices: list[float] = []
    hw = _WEEKLY_PIVOT_HALF_WINDOW
    for i in range(hw, n - hw):
        if highs[i] >= highs[i - hw: i + hw + 1].max():
            prices.append(float(highs[i]))
        if lows[i] <= lows[i - hw: i + hw + 1].min():
            prices.append(float(lows[i]))
    return prices


def _matches_weekly_pivot(
    center_price: float,
    weekly_pivot_prices: list[float],
    tolerance_pct: float,
) -> bool:
    """Return True if any weekly pivot sits within tolerance of ``center_price``."""
    if not weekly_pivot_prices or center_price <= 0:
        return False
    threshold = center_price * tolerance_pct / 100.0
    return any(abs(wp - center_price) <= threshold for wp in weekly_pivot_prices)


# ---------------------------------------------------------------------------
# Pivot detection
# ---------------------------------------------------------------------------


def _find_pivots(bars: pd.DataFrame, *, half_window: int) -> list[_Pivot]:
    """Walk the bars and identify pivot highs and lows.

    A pivot HIGH at index ``i`` requires that ``high[i]`` is >= every
    other high in the window ``[i - half_window, i + half_window]``.
    The first and last ``half_window`` bars can't be pivots because
    the window extends past the data.

    Each pivot carries the bar's volume so the cluster scorer can
    blend a volume-weighted touch component. When the input frame has
    no ``volume`` column (synthetic test data or unusual instruments),
    pivot volumes default to 0.0 and the strength score gracefully
    degrades to a count-based touch contribution (zero volume across
    all pivots normalises to zero, so the other components carry the
    score).
    """
    highs = bars["high"].to_numpy(dtype=float)
    lows = bars["low"].to_numpy(dtype=float)
    volumes = (
        bars["volume"].to_numpy(dtype=float)
        if "volume" in bars.columns
        else None
    )
    n = len(highs)
    pivots: list[_Pivot] = []
    for i in range(half_window, n - half_window):
        bar_vol = float(volumes[i]) if volumes is not None else 0.0
        # Pivot high?
        window_h = highs[i - half_window : i + half_window + 1]
        if highs[i] >= window_h.max():
            # Prominence: how much price reversed after this pivot.
            # Use the lowest LOW in the window after i as the floor.
            tail_low = float(lows[i : i + half_window + 1].min())
            prom = float(highs[i]) - tail_low
            pivots.append(
                _Pivot(bar_index=i, price=float(highs[i]),
                       kind="high", prominence=prom, volume=bar_vol)
            )
        # Pivot low?
        window_l = lows[i - half_window : i + half_window + 1]
        if lows[i] <= window_l.min():
            tail_high = float(highs[i : i + half_window + 1].max())
            prom = tail_high - float(lows[i])
            pivots.append(
                _Pivot(bar_index=i, price=float(lows[i]),
                       kind="low", prominence=prom, volume=bar_vol)
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
    weekly_pivot_prices: list[float] | None = None,
) -> list[Level]:
    """Single-pass agglomerative clustering of pivots by price proximity.

    Pivots are sorted by price; consecutive pivots within
    ``cluster_tolerance_pct`` of each other merge into one cluster.
    Each cluster's center price is the average of its members, and its
    strength score blends volume-weighted touches, recency, and pivot
    prominence.

    The cluster's ``kind`` ("support" | "resistance") is determined per
    level from where its center sits relative to ``last_close``: a
    pivot-high cluster that price has since broken out above is now a
    *support* level. The original pivot type is preserved in ``origin``
    for the UI to flag the role-reversal.

    When ``weekly_pivot_prices`` is provided, clusters whose center
    sits within ``cluster_tolerance_pct`` of any weekly pivot price
    are marked ``confirmed_by_weekly=True`` and receive a 1.3× strength
    multiplier (capped at 1.0).
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
    # For volume-weighted touches and prominence normalisation, we want
    # a per-run baseline so the scores live in roughly [0, 1].
    cluster_volumes = [sum(q.volume for q in c) for c in clusters]
    max_volume = max(cluster_volumes) if cluster_volumes else 0.0
    max_touches = max((len(c) for c in clusters), default=1)
    max_prom = max(
        (max((q.prominence for q in c), default=0.0) for c in clusters),
        default=0.0,
    ) or 1.0

    for cluster, cluster_vol in zip(clusters, cluster_volumes, strict=True):
        center_price = sum(q.price for q in cluster) / len(cluster)
        touches = len(cluster)
        # Most-recent pivot in the cluster (highest bar index).
        most_recent_idx = max(q.bar_index for q in cluster)
        last_touch_days_ago = n_bars - 1 - most_recent_idx
        # Pivot prominence = max prominence among pivots in the cluster.
        prom = max(q.prominence for q in cluster)

        # Component sub-scores (each in [0, 1]).
        # Touch score is volume-weighted when bar volumes were available
        # at pivot detection time. If every cluster had zero volume
        # (synthetic data, no volume column), fall back to count-based
        # normalisation so the score still discriminates between
        # clusters.
        if max_volume > 0:
            touch_score = min(1.0, cluster_vol / max_volume)
        else:
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

        # Weekly-pivot confirmation: bump strength when the cluster
        # aligns with a pivot on the weekly resample.
        confirmed = _matches_weekly_pivot(
            center_price, weekly_pivot_prices or [], cluster_tolerance_pct
        )
        if confirmed:
            strength = min(1.0, strength * _WEEKLY_CONFIRM_MULTIPLIER)

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
                confirmed_by_weekly=confirmed,
            )
        )

    return levels
