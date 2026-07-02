"""Unit + property tests for the watchlist composite math.

Imports only ``stockscan.sectors.composite.weighted_composite`` (pure, no DB),
so this runs without infrastructure. The crown-jewel invariant mirrors the
sector-composite suite: **no look-ahead** — a level at date ``t`` recomputed on
the truncated prefix ``≤ t`` equals the live value at ``t`` — for *both* the
equal-weight and cap-weight paths.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from stockscan.sectors.composite import weighted_composite


def _dates(n: int):
    return pd.date_range("2024-01-01", periods=n, freq="B")


def test_equal_weight_hand_checked():
    idx = _dates(3)
    closes = pd.DataFrame(
        {"A": [100.0, 110.0, 121.0], "B": [100.0, 90.0, 99.0]}, index=idx
    )
    level = weighted_composite(closes, None, base=100.0, min_members=1)
    # day1: base. day2: (+10% , -10%) mean 0 → 100. day3: (+10%, +10%) → 110.
    assert level.iloc[0] == 100.0
    assert abs(level.iloc[1] - 100.0) < 1e-9
    assert abs(level.iloc[2] - 110.0) < 1e-9


def test_cap_weight_with_equal_weights_matches_equal_weight():
    idx = _dates(5)
    rng = np.random.default_rng(0)
    closes = pd.DataFrame(
        {s: 100 * (1 + rng.normal(0, 0.01, 5)).cumprod() for s in ("A", "B", "C")},
        index=idx,
    )
    eq = weighted_composite(closes, None, base=100.0, min_members=1)
    weights = pd.DataFrame(1.0, index=idx, columns=closes.columns)
    cw = weighted_composite(closes, weights, base=100.0, min_members=1)
    # Constant equal weights ⇒ weighted mean == simple mean (prior-day shift only
    # affects day 1, whose return is NaN/flat in both paths).
    pd.testing.assert_series_equal(eq, cw, check_names=False)


def test_cap_weight_dominant_member_tracks_that_member():
    idx = _dates(4)
    closes = pd.DataFrame(
        {"BIG": [100.0, 105.0, 110.25, 115.7625], "small": [100.0, 50.0, 25.0, 12.5]},
        index=idx,
    )
    # BIG hugely outweighs small at every date.
    weights = pd.DataFrame({"BIG": [1e9] * 4, "small": [1.0] * 4}, index=idx)
    cw = weighted_composite(closes, weights, base=100.0, min_members=1)
    # BIG compounds +5%/day → ~115.7625 by day 4; composite should track it.
    assert abs(cw.iloc[-1] - 115.7625) < 0.5


def test_min_members_holds_flat_when_too_few():
    idx = _dates(3)
    # On day 2 only A has a (non-NaN) return; B starts on day 2 so its day-2
    # return is NaN. With min_members=2 the composite stays flat that day.
    closes = pd.DataFrame(
        {"A": [100.0, 110.0, 121.0], "B": [np.nan, 100.0, 110.0]}, index=idx
    )
    level = weighted_composite(closes, None, base=100.0, min_members=2)
    assert abs(level.iloc[1] - 100.0) < 1e-9  # flat: only 1 member had a return
    # day3: both A (+10%) and B (+10%) present → +10%.
    assert abs(level.iloc[2] - 110.0) < 1e-9


def test_nan_member_dropped_not_propagated():
    idx = _dates(3)
    closes = pd.DataFrame(
        {"A": [100.0, 110.0, 121.0], "B": [100.0, np.nan, 99.0]}, index=idx
    )
    # B's day-2 return is NaN (gap) → day 2 is A-only (+10%).
    level = weighted_composite(closes, None, base=100.0, min_members=1)
    assert abs(level.iloc[1] - 110.0) < 1e-9


def test_no_lookahead_truncation_invariance_equal_and_cap():
    idx = _dates(40)
    rng = np.random.default_rng(42)
    closes = pd.DataFrame(
        {s: 100 * (1 + rng.normal(0, 0.012, 40)).cumprod() for s in ("A", "B", "C", "D")},
        index=idx,
    )
    weights = pd.DataFrame(
        {s: rng.uniform(1e8, 1e10, 40) for s in closes.columns}, index=idx
    )
    for w in (None, weights):
        full = weighted_composite(closes, w, base=100.0, min_members=2)
        for k in (10, 25, 39):
            trunc = weighted_composite(
                closes.iloc[:k], None if w is None else w.iloc[:k],
                base=100.0, min_members=2,
            )
            pd.testing.assert_series_equal(
                full.iloc[:k], trunc, check_names=False,
                obj=f"weighted={'cap' if w is not None else 'eq'} k={k}",
            )


def test_empty_frame_is_safe():
    empty = pd.DataFrame()
    assert weighted_composite(empty, None).empty
