"""Tests for SPY drawdown + correction-gap percentile (cycles/drawdown.py).

Covers:
  * Current drawdown + days-since-ATH on a sculpted series.
  * Episode detection: one choppy bear that dips below −10%, bounces above
    it (but not back to a new high), and dips again counts as ONE episode,
    not two — the ATH-reset rule.
  * Inter-correction gaps + the percentile of the current ongoing gap, and
    that a longer trailing calm stretch ranks at a higher percentile.
  * Degenerate paths: a single episode (no historical gaps → percentile
    None) and a never-breached threshold (available, days_since None).
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from stockscan.cycles.drawdown import (
    _correction_episodes,
    _correction_gap,
    compute_drawdown_state,
)


def _series_from_legs(legs: list[tuple[int, float]], start: str = "2000-01-03") -> pd.Series:
    """Build a daily close Series from (n_days, total_pct_move) legs.

    Each leg moves price linearly by ``total_pct_move`` percent over
    ``n_days`` business days. Concatenated, the legs let us sculpt exact
    drawdowns (e.g. a +20% run to a high, then a −15% correction).
    """
    prices: list[float] = [100.0]
    for n, pct in legs:
        target = prices[-1] * (1.0 + pct / 100.0)
        step = (target - prices[-1]) / n
        for _ in range(n):
            prices.append(prices[-1] + step)
    idx = pd.date_range(start, periods=len(prices), freq="B")
    return pd.Series(prices, index=idx, name="close")


def _dd(series: pd.Series):
    return ((series / series.cummax() - 1.0) * 100.0).to_numpy()


# ---------------------------------------------------------------------------
# Episode detection
# ---------------------------------------------------------------------------
def test_choppy_bear_is_one_episode():
    # Up to a high, then a bear that goes −12%, bounces to −6% (NOT a new
    # high), then −13% again, then fully recovers. That's ONE correction.
    s = _series_from_legs([
        (40, 20.0),    # rally to the ATH
        (10, -12.0),   # dip below −10%
        (8, 7.0),      # bounce — recovers to ~ −5.6%, no new high
        (10, -8.0),    # dip below −10% again (same bear)
        (60, 25.0),    # full recovery to a new high
    ])
    eps = _correction_episodes(_dd(s), 10.0)
    assert len(eps) == 1


def test_two_distinct_corrections_are_two_episodes():
    s = _series_from_legs([
        (40, 20.0),    # ATH #1
        (10, -15.0),   # correction #1
        (60, 30.0),    # new ATH #2 (full recovery + higher)
        (10, -14.0),   # correction #2
        (80, 25.0),    # recovery
    ])
    eps = _correction_episodes(_dd(s), 10.0)
    assert len(eps) == 2
    # Episodes are ordered and non-overlapping.
    assert eps[0][1] < eps[1][0]


def test_shallow_dip_below_threshold_not_counted():
    s = _series_from_legs([(40, 20.0), (10, -7.0), (60, 25.0)])  # only −7%
    assert _correction_episodes(_dd(s), 10.0) == []


# ---------------------------------------------------------------------------
# Gap percentile
# ---------------------------------------------------------------------------
def _three_corrections(tail_calm_days: int) -> pd.Series:
    """Three corrections separated by recoveries, then a calm tail of a
    given length. Tunable tail lets us check the percentile responds."""
    return _series_from_legs([
        (40, 20.0), (10, -15.0),          # corr 1
        (50, 30.0), (10, -14.0),          # corr 2  (gap A)
        (50, 30.0), (10, -13.0),          # corr 3  (gap B)
        (tail_calm_days, 25.0),           # calm stretch → current gap
    ])


def test_gap_percentile_fields_present_and_sane():
    s = _three_corrections(tail_calm_days=120)
    gap = _correction_gap(s, 10.0)
    assert gap.available
    assert gap.days_since is not None and gap.days_since > 0
    assert gap.n_historical_gaps == 2          # 3 episodes → 2 completed gaps
    assert gap.median_gap_days is not None
    assert 0.0 <= gap.gap_percentile <= 100.0


def test_longer_calm_tail_ranks_higher():
    short = _correction_gap(_three_corrections(30), 10.0)
    long = _correction_gap(_three_corrections(400), 10.0)
    assert long.days_since > short.days_since
    assert long.gap_percentile >= short.gap_percentile


def test_single_episode_has_no_percentile():
    s = _series_from_legs([(40, 20.0), (10, -15.0), (200, 30.0)])  # 1 correction
    gap = _correction_gap(s, 10.0)
    assert gap.available
    assert gap.days_since is not None
    assert gap.n_historical_gaps is None
    assert gap.gap_percentile is None


def test_never_breached_threshold():
    s = _series_from_legs([(200, 40.0)])  # monotonic up — never any drawdown
    gap = _correction_gap(s, 10.0)
    assert gap.available
    assert gap.days_since is None
    assert gap.gap_percentile is None


# ---------------------------------------------------------------------------
# Top-level state
# ---------------------------------------------------------------------------
def test_compute_drawdown_state_basic():
    s = _series_from_legs([(40, 20.0), (10, -15.0), (60, 30.0), (20, -5.0)])
    bars = pd.DataFrame({"close": s})
    st = compute_drawdown_state(bars, as_of=s.index[-1].date())
    assert st.available
    assert st.drawdown_pct is not None and st.drawdown_pct < 0  # currently off the high
    assert st.ath_close == float(s.max())
    assert st.days_since_ath is not None
    assert st.correction_10pct.days_since is not None


def test_unavailable_on_empty():
    st = compute_drawdown_state(None, as_of=date(2026, 6, 13))
    assert not st.available
    assert st.correction_10pct.days_since is None
