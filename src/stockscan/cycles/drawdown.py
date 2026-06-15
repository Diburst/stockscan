"""SPY drawdown + days-since-correction.

Two state variables, one little dataclass — both computed by walking
SPY's close history backwards from ``as_of``.

  * Current drawdown from the trailing all-time high — % below ATH +
    days since the ATH was made.
  * Days since the most recent 5%, 10%, and 20% correction — defined
    as the most recent date on which SPY closed >=N% below the
    contemporaneous trailing ATH — together with where that gap ranks
    against the distribution of *historical* gaps between corrections
    in our window (a percentile, so "overdue" means something).

These don't predict anything; they answer "where are we in the cycle?".
Rather than compare the current dry spell to a hardcoded threshold, we
build the empirical distribution of past inter-correction gaps from the
same SPY history and report the current gap's percentile — e.g. a gap at
the 90th percentile really is unusually long; one at the 55th is roughly
typical.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from itertools import pairwise
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import date as _date

    import numpy as np
    import pandas as pd


@dataclass(frozen=True, slots=True)
class CorrectionGap:
    """Days since the most recent close ≥X% below the trailing ATH, plus
    where that gap ranks against the history of gaps between corrections.

    ``gap_percentile`` is the share of *completed* historical gaps that were
    shorter than the current ongoing one (0–100; higher = longer dry spell =
    more genuinely "overdue"). ``n_historical_gaps`` is the sample size
    behind it, and ``median_gap_days`` the historical median — both surfaced
    so the percentile can be read honestly (a percentile over n=3 is noise).
    All gaps are measured in calendar days, matching ``days_since``.
    """

    threshold_pct: float  # 5.0, 10.0, 20.0
    available: bool
    days_since: int | None
    last_correction_date: _date | None
    gap_percentile: float | None = None
    n_historical_gaps: int | None = None
    median_gap_days: int | None = None


@dataclass(frozen=True, slots=True)
class DrawdownState:
    available: bool
    last_close: float | None
    ath_close: float | None
    ath_date: _date | None
    drawdown_pct: float | None  # negative when below ATH; 0 when at ATH
    days_since_ath: int | None
    correction_5pct: CorrectionGap
    correction_10pct: CorrectionGap
    correction_20pct: CorrectionGap

    @classmethod
    def unavailable(cls) -> DrawdownState:
        return cls(
            available=False,
            last_close=None,
            ath_close=None,
            ath_date=None,
            drawdown_pct=None,
            days_since_ath=None,
            correction_5pct=CorrectionGap(5.0, False, None, None),
            correction_10pct=CorrectionGap(10.0, False, None, None),
            correction_20pct=CorrectionGap(20.0, False, None, None),
        )


def compute_drawdown_state(
    spy_bars: pd.DataFrame | None,
    as_of: _date,
) -> DrawdownState:
    """Walk SPY's history to compute drawdown + correction gaps."""
    import pandas as pd  # lazy

    if spy_bars is None or spy_bars.empty or "close" not in spy_bars.columns:
        return DrawdownState.unavailable()

    df = spy_bars.copy()
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index, utc=True)
    closes = df["close"].sort_index()
    if closes.empty:
        return DrawdownState.unavailable()

    last_close = float(closes.iloc[-1])
    last_date = closes.index[-1].date()

    # All-time high so far (in our window).
    ath_close = float(closes.max())
    ath_idx = closes.idxmax()
    ath_date = ath_idx.date()

    drawdown_pct = float((last_close / ath_close - 1.0) * 100) if ath_close > 0 else None
    days_since_ath = (last_date - ath_date).days

    # Days since each correction threshold.
    correction_5 = _correction_gap(closes, 5.0)
    correction_10 = _correction_gap(closes, 10.0)
    correction_20 = _correction_gap(closes, 20.0)

    return DrawdownState(
        available=True,
        last_close=last_close,
        ath_close=ath_close,
        ath_date=ath_date,
        drawdown_pct=drawdown_pct,
        days_since_ath=days_since_ath,
        correction_5pct=correction_5,
        correction_10pct=correction_10,
        correction_20pct=correction_20,
    )


def _correction_gap(closes, threshold_pct: float) -> CorrectionGap:
    """Days since the last ≥``threshold_pct`` correction + its percentile.

    Identifies discrete correction *episodes* (so one long bear isn't
    double-counted as many corrections), measures the calendar-day gaps
    between consecutive episodes, and ranks the current ongoing gap against
    that historical distribution. ``days_since`` itself is unchanged — it's
    still calendar days since the most recent under-water bar.
    """
    cum_max = closes.cummax()
    dd = ((closes / cum_max - 1.0) * 100.0).to_numpy()  # negative below ATH
    episodes = _correction_episodes(dd, threshold_pct)
    if not episodes:
        # Insufficient history or never breached — convey "no correction at
        # this threshold in our history".
        return CorrectionGap(threshold_pct, True, None, None)

    index = closes.index
    last_close_date = index[-1].date()
    # Most recent episode's last under-water bar = the "last correction" bar.
    last_corr_date = index[episodes[-1][1]].date()
    days_since = (last_close_date - last_corr_date).days

    # Completed historical gaps: end of one episode → start of the next.
    gaps = [
        (index[nxt[0]].date() - index[prev[1]].date()).days
        for prev, nxt in pairwise(episodes)
    ]
    gap_percentile: float | None = None
    median_gap: int | None = None
    if gaps:
        median_gap = round(statistics.median(gaps))
        gap_percentile = sum(1 for g in gaps if g <= days_since) / len(gaps) * 100.0

    return CorrectionGap(
        threshold_pct=threshold_pct,
        available=True,
        days_since=days_since,
        last_correction_date=last_corr_date,
        gap_percentile=round(gap_percentile, 1) if gap_percentile is not None else None,
        n_historical_gaps=len(gaps) if gaps else None,
        median_gap_days=median_gap,
    )


def _correction_episodes(dd: np.ndarray, threshold_pct: float) -> list[tuple[int, int]]:
    """Group bars into discrete correction episodes as (start_i, end_i).

    A bar is "in correction" when its drawdown from the trailing ATH is
    ≤ −``threshold_pct``. Consecutive in-correction bars belong to the same
    episode; the episode only *ends* once price recovers to within half the
    threshold of the high (drawdown back above −``threshold_pct``/2). That
    reset rule keeps a single choppy bear — which can dip below the line,
    bounce, and dip again — as one correction rather than several, while
    still separating genuinely distinct corrections.

    ``start_i`` is the first under-water bar; ``end_i`` is the last
    under-water bar of that episode. A trailing episode still under water at
    the end of the data is included (its ``end_i`` is the final breach bar).
    Pure index math over a NumPy array — one cheap pass, no pandas per-cell.
    """
    reset = -threshold_pct / 2.0
    episodes: list[tuple[int, int]] = []
    in_episode = False
    start_i = end_i = 0
    for i in range(dd.shape[0]):
        d = dd[i]
        if d <= -threshold_pct:
            if not in_episode:
                in_episode = True
                start_i = i
            end_i = i
        elif in_episode and d >= reset:
            episodes.append((start_i, end_i))
            in_episode = False
    if in_episode:
        episodes.append((start_i, end_i))
    return episodes
