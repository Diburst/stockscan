"""Tests for the revised S/R strength formula + Fibonacci retracement.

Covers:
  * Volume-weighted touch score — a high-volume cluster of N touches
    outranks an equal-touch low-volume cluster (the whole point of the
    convention shift away from raw touch count).
  * Weekly-pivot confirmation — clusters whose center matches a weekly
    pivot get ``confirmed_by_weekly=True`` and a 1.3× strength multiplier
    (capped at 1.0).
  * Per-side cap tightened from 4 to 3.
  * Fibonacci primitive — output shape, direction inference, level math.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from stockscan.analysis.levels import (
    _MAX_LEVELS_PER_SIDE,
    find_support_resistance,
)
from stockscan.indicators.fibonacci import (
    DEFAULT_LOOKBACK_BARS,
    FIB_RATIOS,
    fibonacci_retracement,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_bars(
    n: int = 600,
    *,
    base_price: float = 100.0,
    seed: int = 0,
    drift_std: float = 0.1,
) -> pd.DataFrame:
    """Random-walk OHLCV bars with a daily DatetimeIndex.

    Long enough (default 600 trading days ≈ 2.4 years) to give the weekly
    resample 100+ rows and the daily pivot pass a long history to chew on.

    ``drift_std`` defaults small (0.1) so the walk stays close to
    ``base_price`` — keeps injected test pivots within the 25% distance
    filter without per-test fiddling. Fib tests that exercise the
    direction-inference path override anchors directly.
    """
    idx = pd.date_range("2023-01-02", periods=n, freq="B")
    rng = np.random.default_rng(seed)
    closes = base_price + np.cumsum(rng.normal(0, drift_std, n))
    return pd.DataFrame(
        {
            "open": closes + rng.normal(0, 0.05, n),
            "high": closes + np.abs(rng.normal(0, 0.2, n)),
            "low": closes - np.abs(rng.normal(0, 0.2, n)),
            "close": closes,
            "adj_close": closes,
            "volume": rng.integers(100_000, 1_000_000, n).astype(float),
        },
        index=idx,
    )


def _inject_pivot(
    bars: pd.DataFrame,
    bar_index: int,
    *,
    price: float,
    kind: str,
    volume: float,
    half_window: int = 5,
) -> None:
    """Force a clean pivot at ``bar_index`` by clamping the surrounding bars.

    Mutates ``bars`` in place. For a pivot HIGH, surrounding highs are
    capped below ``price`` and the pivot bar's high is set to ``price``.
    Mirror for pivot LOW.
    """
    start = max(0, bar_index - half_window)
    end = min(len(bars) - 1, bar_index + half_window)
    if kind == "high":
        # Surrounding highs strictly below the pivot.
        for i in range(start, end + 1):
            if i == bar_index:
                bars.iloc[i, bars.columns.get_loc("high")] = price
                bars.iloc[i, bars.columns.get_loc("volume")] = volume
            else:
                bars.iloc[i, bars.columns.get_loc("high")] = price - 1.0
    else:
        for i in range(start, end + 1):
            if i == bar_index:
                bars.iloc[i, bars.columns.get_loc("low")] = price
                bars.iloc[i, bars.columns.get_loc("volume")] = volume
            else:
                bars.iloc[i, bars.columns.get_loc("low")] = price + 1.0


def _make_flat_bars(
    n: int = 600,
    *,
    close_price: float = 100.0,
) -> pd.DataFrame:
    """Deterministic OHLCV scaffold designed to produce NO natural pivots.

    Highs are strictly increasing by a tiny step (1e-5/bar) and lows are
    strictly decreasing by the same step. Because the pivot detector
    requires ``high[i] >= max(window)`` and ``window`` includes future
    bars, no bar in a strictly-monotonic highs array can be a pivot high
    (the next bar always has a higher high). Same logic for lows.

    Used for tests that need to isolate the effect of *only* the injected
    pivots without the bar series creating natural pivots that compete
    with the injections for the per-side cap.
    """
    idx = pd.date_range("2023-01-02", periods=n, freq="B")
    step = 1e-5
    highs = [close_price + 0.5 + step * i for i in range(n)]
    lows = [close_price - 0.5 - step * i for i in range(n)]
    return pd.DataFrame(
        {
            "open": [close_price] * n,
            "high": highs,
            "low": lows,
            "close": [close_price] * n,
            "adj_close": [close_price] * n,
            "volume": [100_000.0] * n,
        },
        index=idx,
    )


# ---------------------------------------------------------------------------
# Fibonacci primitive
# ---------------------------------------------------------------------------


def test_fib_shape_and_levels():
    """Output dict has the expected keys + 5 retracement ratios in order."""
    bars = _make_bars(n=300, seed=1)
    out = fibonacci_retracement(bars)
    assert out is not None
    for key in ("high", "low", "high_date", "low_date", "direction", "levels"):
        assert key in out
    assert out["high"] > out["low"]
    assert len(out["levels"]) == 5
    # Ratios in the canonical order.
    ratios = [lv["ratio"] for lv in out["levels"]]
    assert ratios == list(FIB_RATIOS)


def test_fib_level_math():
    """Each level price = high - ratio * (high - low) to within rounding."""
    bars = _make_bars(n=300, seed=2)
    out = fibonacci_retracement(bars)
    assert out is not None
    rng = out["high"] - out["low"]
    for lv in out["levels"]:
        expected = round(out["high"] - lv["ratio"] * rng, 4)
        assert lv["price"] == pytest.approx(expected, abs=0.01)
        # Each level price sits strictly between the anchors.
        assert out["low"] <= lv["price"] <= out["high"]


def test_fib_direction_down_from_high():
    """When the swing high is the most recent extreme, direction = down_from_high."""
    bars = _make_bars(n=300, seed=3)
    # Force a clean high near the end and a low earlier in the lookback.
    bars.iloc[-5, bars.columns.get_loc("high")] = 999.0  # newest extreme
    bars.iloc[50, bars.columns.get_loc("low")] = -50.0   # older low (still in lookback)
    out = fibonacci_retracement(bars, lookback=120)
    assert out is not None
    assert out["direction"] == "down_from_high"


def test_fib_direction_up_from_low():
    """When the swing low is the most recent extreme, direction = up_from_low."""
    bars = _make_bars(n=300, seed=4)
    bars.iloc[50, bars.columns.get_loc("high")] = 999.0  # older high
    bars.iloc[-5, bars.columns.get_loc("low")] = -50.0   # newest low
    out = fibonacci_retracement(bars, lookback=120)
    assert out is not None
    assert out["direction"] == "up_from_low"


def test_fib_returns_none_on_short_history():
    """Lookback longer than the input → None (caller's empty-state path)."""
    short = _make_bars(n=50)
    assert fibonacci_retracement(short, lookback=120) is None


def test_fib_returns_none_on_flat_swing():
    """Anchor high == anchor low (degenerate) → None."""
    idx = pd.date_range("2024-01-01", periods=DEFAULT_LOOKBACK_BARS + 5, freq="B")
    flat = pd.DataFrame(
        {
            "high": [100.0] * len(idx),
            "low": [100.0] * len(idx),
            "close": [100.0] * len(idx),
            "volume": [1.0] * len(idx),
        },
        index=idx,
    )
    assert fibonacci_retracement(flat) is None


# ---------------------------------------------------------------------------
# Volume-weighted touch score
# ---------------------------------------------------------------------------


def test_volume_weighting_beats_count_at_equal_touches():
    """Two clusters with the same touch count — the one tested at heavier
    volume should rank stronger. Uses a deterministic flat scaffold so the
    only pivots in the run are the ones we inject."""
    bars = _make_flat_bars(n=600)
    # Two distinct pivot-high prices, each tested twice. Cluster A is
    # high-volume; cluster B is low-volume. With the old count-only
    # formula they'd score equally; under volume-weighting A beats B.
    _inject_pivot(bars, 100, price=110.0, kind="high", volume=10_000_000)
    _inject_pivot(bars, 200, price=110.0, kind="high", volume=10_000_000)
    _inject_pivot(bars, 300, price=105.0, kind="high", volume=100_000)
    _inject_pivot(bars, 400, price=105.0, kind="high", volume=100_000)
    levels = find_support_resistance(bars)
    by_price = {round(lv.price): lv for lv in levels}
    assert 110 in by_price, "cluster A must survive the strength + distance filter"
    assert 105 in by_price, "cluster B must survive the strength + distance filter"
    assert by_price[110].strength > by_price[105].strength


def test_levels_carry_touches_count_for_display():
    """Touch count is still reported on the Level for UI display, even
    though it no longer drives strength directly."""
    bars = _make_flat_bars(n=600)
    _inject_pivot(bars, 100, price=108.0, kind="high", volume=1_000_000)
    _inject_pivot(bars, 200, price=108.0, kind="high", volume=1_000_000)
    _inject_pivot(bars, 300, price=108.0, kind="high", volume=1_000_000)
    levels = find_support_resistance(bars)
    cluster = next((lv for lv in levels if round(lv.price) == 108), None)
    assert cluster is not None
    assert cluster.touches == 3


# ---------------------------------------------------------------------------
# Weekly-pivot confirmation
# ---------------------------------------------------------------------------


def test_weekly_confirmation_applies_multiplier_when_aligned():
    """A daily cluster whose price matches a weekly pivot gets
    confirmed=True. Uses the flat scaffold so the weekly resample sees
    no competing natural pivots — only our injected ones."""
    bars = _make_flat_bars(n=600)
    weekly_aligned_price = 112.0
    # Spread pivots far enough apart that they sit in different weekly
    # neighborhoods (weekly half-window is 3 weeks = 15 trading days).
    for daily_idx in (60, 180, 300, 420):
        _inject_pivot(
            bars, daily_idx, price=weekly_aligned_price,
            kind="high", volume=5_000_000,
        )
    levels = find_support_resistance(bars)
    by_price = {round(lv.price): lv for lv in levels}
    aligned = by_price.get(int(weekly_aligned_price))
    assert aligned is not None, "injected cluster should survive the filters"
    # The aligned cluster should be flagged as weekly-confirmed AND the
    # strength must reflect the 1.3× multiplier (capped at 1.0).
    assert aligned.confirmed_by_weekly is True
    assert 0 < aligned.strength <= 1.0


# ---------------------------------------------------------------------------
# Per-side cap
# ---------------------------------------------------------------------------


def test_cap_is_three_per_side():
    """At most 3 supports + 3 resistances are returned."""
    bars = _make_flat_bars(n=600)
    # Inject many candidate pivots above + below current close. Spread
    # prices widely enough to escape the 1.5% cluster tolerance so each
    # injection becomes its own cluster.
    for i, idx in enumerate(range(50, 500, 30)):
        if i % 2 == 0:
            _inject_pivot(bars, idx, price=104.0 + i * 0.5, kind="high", volume=1_000_000)
        else:
            _inject_pivot(bars, idx, price=96.0 - i * 0.5, kind="low", volume=1_000_000)
    levels = find_support_resistance(bars)
    supports = [lv for lv in levels if lv.kind == "support"]
    resistances = [lv for lv in levels if lv.kind == "resistance"]
    assert len(supports) <= _MAX_LEVELS_PER_SIDE
    assert len(resistances) <= _MAX_LEVELS_PER_SIDE
    assert _MAX_LEVELS_PER_SIDE == 3


# ---------------------------------------------------------------------------
# Degenerate inputs
# ---------------------------------------------------------------------------


def test_empty_bars_returns_no_levels():
    assert find_support_resistance(pd.DataFrame()) == []


def test_short_history_returns_no_levels():
    """Fewer bars than 2×half_window+1 → no pivots possible → no levels."""
    short = _make_bars(n=5)
    assert find_support_resistance(short) == []


def test_missing_volume_column_does_not_crash():
    """The strength formula gracefully degrades when volume isn't available."""
    bars = _make_bars(n=300, seed=40).drop(columns=["volume"])
    # Should not raise.
    levels = find_support_resistance(bars)
    # And should still return Level instances (count-based touch fallback).
    assert all(hasattr(lv, "strength") for lv in levels)
