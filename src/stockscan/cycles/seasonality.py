"""Data-driven seasonal indicators.

All three of these compute their stats from the SPY bars passed in,
NOT from hardcoded reference tables. So the "since YYYY" qualifier in
the rendered card reflects how much history is actually in your DB
right now.

Three indicators:
  * :func:`monthly_seasonality` — for the current calendar month, the
    historical green/red rate and average return.
  * :func:`halloween_window_stats` — current window (May-Oct or Nov-Apr)
    + the previous full window's return.
  * :func:`january_barometer` — sign of January's return as a "tell"
    for the full year, with historical hit-rate.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import date as _date

    import pandas as pd


# Names indexed 1-12 so we can look them up via ``date.month`` directly.
# (calendar.month_name[0] is empty by convention.)
_MONTH_NAMES = list(calendar.month_name)


# ---------------------------------------------------------------------------
# Monthly seasonality
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MonthlySeasonalityState:
    """The current calendar month's historical bias."""

    as_of: _date
    available: bool
    month_name: str
    n_observations: int  # how many years of history fed into the stats
    n_positive: int  # of those, how many years had a positive month return
    avg_return_pct: float | None  # mean monthly return, in percent (e.g. 1.4)
    median_return_pct: float | None
    best_return_pct: float | None
    worst_return_pct: float | None
    earliest_year: int | None  # for "since YYYY" labelling
    latest_year: int | None

    @classmethod
    def unavailable(cls, as_of: _date) -> MonthlySeasonalityState:
        return cls(
            as_of=as_of,
            available=False,
            month_name=_MONTH_NAMES[as_of.month],
            n_observations=0,
            n_positive=0,
            avg_return_pct=None,
            median_return_pct=None,
            best_return_pct=None,
            worst_return_pct=None,
            earliest_year=None,
            latest_year=None,
        )

    @property
    def positive_rate(self) -> float | None:
        if self.n_observations <= 0:
            return None
        return self.n_positive / self.n_observations


def monthly_seasonality(
    spy_bars: pd.DataFrame | None,
    as_of: _date,
) -> MonthlySeasonalityState:
    """Aggregate SPY's history by (year, month) and slice to ``as_of.month``.

    Excludes the current in-progress month (only fully-completed months
    contribute to the bias) so a partial-month return doesn't pollute
    the stat. If we have fewer than 3 prior observations of the current
    month, we return ``available=False`` rather than print a noisy stat.
    """
    if spy_bars is None or spy_bars.empty:
        return MonthlySeasonalityState.unavailable(as_of)

    monthly_returns = _aggregate_monthly_returns(spy_bars)
    if monthly_returns is None or monthly_returns.empty:
        return MonthlySeasonalityState.unavailable(as_of)

    target_month = as_of.month
    # Drop the current in-progress month if it's in the index.
    current_idx = (as_of.year, target_month)
    history = monthly_returns.drop(index=current_idx, errors="ignore")
    history = history[history.index.get_level_values("month") == target_month]

    n_obs = len(history)
    if n_obs < 3:
        return MonthlySeasonalityState.unavailable(as_of)

    n_pos = int((history > 0).sum())
    return MonthlySeasonalityState(
        as_of=as_of,
        available=True,
        month_name=_MONTH_NAMES[target_month],
        n_observations=n_obs,
        n_positive=n_pos,
        avg_return_pct=float(history.mean() * 100),
        median_return_pct=float(history.median() * 100),
        best_return_pct=float(history.max() * 100),
        worst_return_pct=float(history.min() * 100),
        earliest_year=int(history.index.get_level_values("year").min()),
        latest_year=int(history.index.get_level_values("year").max()),
    )


# ---------------------------------------------------------------------------
# Halloween window
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HalloweenState:
    """Whether we're in the strong (Nov-Apr) or weak (May-Oct) period."""

    as_of: _date
    available: bool
    in_winter_window: bool  # True for Nov-Apr ("buy"); False for May-Oct ("sell")
    window_label: str
    # Aggregate over local history: average return for THIS window across
    # all completed years on file.
    historical_avg_pct: float | None
    historical_positive_rate: float | None
    n_observations: int
    earliest_year: int | None
    latest_year: int | None

    @classmethod
    def unavailable(cls, as_of: _date) -> HalloweenState:
        in_winter = as_of.month >= 11 or as_of.month <= 4
        return cls(
            as_of=as_of,
            available=False,
            in_winter_window=in_winter,
            window_label="Nov-Apr (buy)" if in_winter else "May-Oct (sell)",
            historical_avg_pct=None,
            historical_positive_rate=None,
            n_observations=0,
            earliest_year=None,
            latest_year=None,
        )


def halloween_window_stats(
    spy_bars: pd.DataFrame | None,
    as_of: _date,
) -> HalloweenState:
    """Compute average / positivity of the current 6-month window from history.

    "Winter window" runs Nov 1 to Apr 30 (the strong half per
    Bouman & Jacobsen). We compute, across all complete winter and
    summer windows on file, the average return for whichever window
    we're currently in.
    """
    in_winter = as_of.month >= 11 or as_of.month <= 4
    state = HalloweenState.unavailable(as_of)
    if spy_bars is None or spy_bars.empty:
        return state

    # Build month-end close series for window math.
    monthly_close = _monthly_close_series(spy_bars)
    if monthly_close is None or len(monthly_close) < 24:
        return state

    # Walk year-by-year computing each window's return.
    returns: list[float] = []
    years_seen: list[int] = []
    # Iterate over all complete years of data we have.
    earliest_year = int(monthly_close.index[0].year)
    latest_year = int(monthly_close.index[-1].year)
    for year in range(earliest_year, latest_year + 1):
        if in_winter:
            # Winter window: Nov of (year-1) through Apr of year.
            start_marker = (year - 1, 10)  # use Oct close as "start of Nov"
            end_marker = (year, 4)  # Apr close = end of winter window
        else:
            # Summer window: May through Oct of year.
            start_marker = (year, 4)  # Apr close = start of May
            end_marker = (year, 10)  # Oct close = end of summer window

        start_close = _close_at_month_end(monthly_close, *start_marker)
        end_close = _close_at_month_end(monthly_close, *end_marker)
        if start_close is None or end_close is None or start_close <= 0:
            continue
        returns.append((end_close / start_close) - 1.0)
        years_seen.append(year)

    if len(returns) < 3:
        return state

    n_pos = sum(1 for r in returns if r > 0)
    return HalloweenState(
        as_of=as_of,
        available=True,
        in_winter_window=in_winter,
        window_label="Nov-Apr (buy)" if in_winter else "May-Oct (sell)",
        historical_avg_pct=float(sum(returns) / len(returns) * 100),
        historical_positive_rate=n_pos / len(returns),
        n_observations=len(returns),
        earliest_year=min(years_seen),
        latest_year=max(years_seen),
    )


# ---------------------------------------------------------------------------
# January Barometer
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class JanuaryBarometerState:
    """'As goes January, so goes the year' — sign-match hit rate."""

    as_of: _date
    available: bool
    current_year: int
    january_complete: bool
    january_return_pct: float | None  # this year's January, if complete
    january_was_positive: bool | None
    # Hit rate over all completed prior years on file.
    sign_match_rate: float | None  # 0..1, fraction of years where sign(Jan) == sign(year)
    n_observations: int
    earliest_year: int | None

    @classmethod
    def unavailable(cls, as_of: _date) -> JanuaryBarometerState:
        return cls(
            as_of=as_of,
            available=False,
            current_year=as_of.year,
            january_complete=as_of.month >= 2,
            january_return_pct=None,
            january_was_positive=None,
            sign_match_rate=None,
            n_observations=0,
            earliest_year=None,
        )


def january_barometer(
    spy_bars: pd.DataFrame | None,
    as_of: _date,
) -> JanuaryBarometerState:
    """Compute Jan-direction-vs-year hit rate from local SPY bars.

    We only count years where BOTH January and the full year are
    complete (so the most recent year only contributes if as_of is
    in a future calendar year, or after Dec 31). Current year's Jan
    is shown separately as a "tell" if January is complete.
    """
    if spy_bars is None or spy_bars.empty:
        return JanuaryBarometerState.unavailable(as_of)

    monthly_close = _monthly_close_series(spy_bars)
    if monthly_close is None or len(monthly_close) < 24:
        return JanuaryBarometerState.unavailable(as_of)

    earliest_year = int(monthly_close.index[0].year)

    # Current year's January (if complete: as_of.month >= 2).
    current_jan_pct: float | None = None
    current_jan_pos: bool | None = None
    if as_of.month >= 2:
        # Year-end of prior calendar year vs Jan-end of this one.
        prev_dec = _close_at_month_end(monthly_close, as_of.year - 1, 12)
        this_jan = _close_at_month_end(monthly_close, as_of.year, 1)
        if prev_dec and this_jan and prev_dec > 0:
            current_jan_pct = float((this_jan / prev_dec - 1.0) * 100)
            current_jan_pos = current_jan_pct > 0

    # Historical hit rate: years where year is fully complete (i.e.,
    # we have a Dec close for that year AND the next year exists).
    matches = 0
    total = 0
    for year in range(earliest_year + 1, as_of.year):  # excludes current year
        prev_dec = _close_at_month_end(monthly_close, year - 1, 12)
        jan_close = _close_at_month_end(monthly_close, year, 1)
        dec_close = _close_at_month_end(monthly_close, year, 12)
        if prev_dec is None or jan_close is None or dec_close is None:
            continue
        if prev_dec <= 0:
            continue
        jan_pos = jan_close > prev_dec
        year_pos = dec_close > prev_dec
        total += 1
        if jan_pos == year_pos:
            matches += 1

    if total < 3:
        # Fall through with whatever current-Jan we computed; mark
        # historical part as unavailable.
        return JanuaryBarometerState(
            as_of=as_of,
            available=current_jan_pct is not None,
            current_year=as_of.year,
            january_complete=as_of.month >= 2,
            january_return_pct=current_jan_pct,
            january_was_positive=current_jan_pos,
            sign_match_rate=None,
            n_observations=total,
            earliest_year=earliest_year if total else None,
        )

    return JanuaryBarometerState(
        as_of=as_of,
        available=True,
        current_year=as_of.year,
        january_complete=as_of.month >= 2,
        january_return_pct=current_jan_pct,
        january_was_positive=current_jan_pos,
        sign_match_rate=matches / total,
        n_observations=total,
        earliest_year=earliest_year,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _aggregate_monthly_returns(spy_bars: pd.DataFrame):
    """Compute end-of-month returns indexed by (year, month).

    Returns a pandas Series with a 2-level MultiIndex (year, month)
    or None if the bars frame is unusable.
    """
    import pandas as pd  # lazy — keeps module import light when unused

    df = spy_bars.copy()
    if "close" not in df.columns:
        return None
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index, utc=True)
    # Resample to month-end closes.
    me = df["close"].resample("ME").last().dropna()
    if me.empty:
        return None
    rets = me.pct_change().dropna()
    rets.index = pd.MultiIndex.from_arrays(
        [rets.index.year, rets.index.month], names=["year", "month"]
    )
    return rets


def _monthly_close_series(spy_bars: pd.DataFrame):
    """Return a Series of last close per month, indexed by month-end timestamp."""
    import pandas as pd

    df = spy_bars
    if "close" not in df.columns:
        return None
    if not isinstance(df.index, pd.DatetimeIndex):
        idx = pd.to_datetime(df.index, utc=True)
    else:
        idx = df.index
    s = df["close"].copy()
    s.index = idx
    me = s.resample("ME").last().dropna()
    return me if not me.empty else None


def _close_at_month_end(monthly_close, year: int, month: int) -> float | None:
    """Look up the month-end close for (year, month) from a resampled series."""
    matches = monthly_close[
        (monthly_close.index.year == year) & (monthly_close.index.month == month)
    ]
    if len(matches) == 0:
        return None
    return float(matches.iloc[-1])
