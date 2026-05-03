"""Date-window indicators — pure date checks, no price data needed.

Two indicators here: turn-of-month and Santa Claus rally. Both are
just "are we currently inside this calendar window?" with a small
explanation on hover.

Reference values (for the tooltip / education):
  * Turn-of-month effect — Ariel (1987), Lakonishok-Smidt (1988).
    Documented since the 1950s; effect roughly halved post-1990s but
    still present.
  * Santa Claus rally — Yale Hirsch first documented in *Stock Trader's
    Almanac*. Last 5 trading days of December + first 2 of January
    historically positive ~78% of the time with ~1.4% average return.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date as _date
from datetime import timedelta as _td

# ---------------------------------------------------------------------------
# Turn-of-month
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TurnOfMonthState:
    """In/out of the historical month-turn window.

    Window definition: the LAST trading day of the prior month + the
    first FOUR trading days of the current month. We approximate
    "trading day" with weekdays (Mon-Fri) — close enough for daily
    bar dashboards; holidays are off by a day at most.
    """

    available: bool
    in_window: bool
    days_into_window: int | None  # 0..4 if in window, else None
    next_window_start: _date | None  # if currently OUTSIDE the window
    explanation: str

    @classmethod
    def unavailable(cls) -> TurnOfMonthState:
        return cls(
            available=False,
            in_window=False,
            days_into_window=None,
            next_window_start=None,
            explanation="",
        )


def turn_of_month_window(as_of: _date) -> TurnOfMonthState:
    # Last trading day of the prior month.
    last_of_prior = _last_weekday_of_month(as_of.year, as_of.month - 1 or 12,
                                           year_offset=(0 if as_of.month != 1 else -1))
    # First 4 trading days of current month.
    first_four = _first_n_weekdays(as_of.year, as_of.month, 4)
    window_dates = {last_of_prior, *first_four}

    if as_of in window_dates:
        sorted_dates = sorted(window_dates)
        idx = sorted_dates.index(as_of)
        return TurnOfMonthState(
            available=True,
            in_window=True,
            days_into_window=idx,  # 0 = last-of-prior, 1..4 = first 4 of new month
            next_window_start=None,
            explanation=(
                "Last day of the prior month plus the first four trading "
                "days of this month. Historically captures most of the "
                "monthly return; effect documented by Ariel (1987) and "
                "Lakonishok-Smidt (1988)."
            ),
        )

    # Compute next window start.
    next_year, next_month = (
        (as_of.year, as_of.month + 1)
        if as_of.month < 12
        else (as_of.year + 1, 1)
    )
    next_start = _last_weekday_of_month(
        next_year if next_month != 1 else next_year - 1,
        next_month - 1 if next_month > 1 else 12,
    )
    if next_start <= as_of:
        # We're past the prior month-end; the next window starts at
        # the last weekday of THIS month.
        next_start = _last_weekday_of_month(as_of.year, as_of.month)
    return TurnOfMonthState(
        available=True,
        in_window=False,
        days_into_window=None,
        next_window_start=next_start,
        explanation=(
            "Outside the turn-of-month window. The window opens on "
            "the last trading day of this month."
        ),
    )


# ---------------------------------------------------------------------------
# Santa Claus rally
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SantaClausState:
    """Last 5 trading days of December + first 2 trading days of January."""

    available: bool
    in_window: bool
    days_into_window: int | None  # 0..6 if in window
    next_window_start: _date | None  # always populated unless we're in it
    explanation: str

    @classmethod
    def unavailable(cls) -> SantaClausState:
        return cls(
            available=False,
            in_window=False,
            days_into_window=None,
            next_window_start=None,
            explanation="",
        )


def santa_claus_window(as_of: _date) -> SantaClausState:
    # The Santa window straddles a calendar year boundary (Dec → Jan),
    # so depending on when `as_of` falls we need to check the window
    # centred on (this_year - 1 → this_year), the one centred on
    # (this_year → this_year + 1), AND a year-later one for the
    # next-upcoming-start lookup when `as_of` falls between windows
    # (e.g., Dec 25 itself, which is a market holiday so it sits
    # inside no window).
    candidate_windows = [
        _santa_window_for(as_of.year),       # late Dec (yr-1) + early Jan (yr)
        _santa_window_for(as_of.year + 1),   # late Dec (yr)   + early Jan (yr+1)
        _santa_window_for(as_of.year + 2),   # late Dec (yr+1) + early Jan (yr+2)
    ]

    for window in candidate_windows:
        if as_of in window:
            idx = sorted(window).index(as_of)
            return SantaClausState(
                available=True,
                in_window=True,
                days_into_window=idx,
                next_window_start=None,
                explanation=(
                    "Last 5 trading days of December + first 2 trading days "
                    "of January. Historically positive ~78% of the time per "
                    "Hirsch's *Stock Trader's Almanac*."
                ),
            )

    # Outside any window — pick the next upcoming start.
    candidates = sorted(min(w) for w in candidate_windows)
    upcoming = next((d for d in candidates if d > as_of), None)
    return SantaClausState(
        available=True,
        in_window=False,
        days_into_window=None,
        next_window_start=upcoming,
        explanation="Outside the Santa Claus rally window.",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# US market holidays that fall inside the Santa window. We don't have
# a market-calendar lib so we hardcode just the two that overlap the
# late-December → early-January span. Other US holidays (Thanksgiving,
# July 4th, etc) don't affect any of the windows on this dashboard.
def _is_market_holiday(d: _date) -> bool:
    return (d.month, d.day) in {(12, 25), (1, 1)}


def _is_trading_day(d: _date) -> bool:
    return d.weekday() < 5 and not _is_market_holiday(d)


def _last_weekday_of_month(year: int, month: int, year_offset: int = 0) -> _date:
    """The last Mon-Fri of (year + year_offset, month)."""
    yr = year + year_offset
    last_day = calendar.monthrange(yr, month)[1]
    d = _date(yr, month, last_day)
    while d.weekday() >= 5:  # Sat/Sun → step back
        d -= _td(days=1)
    return d


def _first_n_weekdays(year: int, month: int, n: int) -> list[_date]:
    """The first ``n`` weekdays of the (year, month)."""
    out: list[_date] = []
    d = _date(year, month, 1)
    while len(out) < n and d.month == month:
        if d.weekday() < 5:
            out.append(d)
        d += _td(days=1)
    return out


def _santa_window_for(year: int) -> list[_date]:
    """The 7-day Santa window centred on the year-boundary (year-1 → year).

    Last 5 trading days of December year-1 + first 2 trading days of
    January year. We use the trading-day filter (weekday AND not a
    market holiday) for this window because the two holidays we care
    about — Christmas and New Year's Day — fall right inside it.
    """
    # Last 5 trading days of December (year - 1)
    dec_last = _date(year - 1, 12, 31)
    last5: list[_date] = []
    d = dec_last
    while len(last5) < 5:
        if _is_trading_day(d):
            last5.append(d)
        d -= _td(days=1)
    last5.sort()

    # First 2 trading days of January (year)
    first2: list[_date] = []
    d = _date(year, 1, 1)
    while len(first2) < 2 and d.month == 1:
        if _is_trading_day(d):
            first2.append(d)
        d += _td(days=1)
    return last5 + first2
