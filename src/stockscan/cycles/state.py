"""CalendarState — the bundle the dashboard reads, plus the orchestrator.

One entry point: :func:`compute_calendar_state`. Returns a
:class:`CalendarState` with one named field per indicator, each a
focused little dataclass that can render itself in the template.

Every sub-computation is wrapped in its own try/except so any single
indicator failing (e.g., insufficient SPY bars history, a benchmark
fetch that times out) returns its sentinel "unavailable" form rather
than blanking out the whole card.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date as _date
from typing import TYPE_CHECKING

from stockscan.cycles.breadth import BreadthState, compute_breadth
from stockscan.cycles.cycles import (
    DecennialState,
    PresidentialCycleState,
    decennial_state,
    presidential_cycle_state,
)
from stockscan.cycles.drawdown import (
    DrawdownState,
    compute_drawdown_state,
)
from stockscan.cycles.seasonality import (
    HalloweenState,
    JanuaryBarometerState,
    MonthlySeasonalityState,
    halloween_window_stats,
    january_barometer,
    monthly_seasonality,
)
from stockscan.cycles.windows import (
    SantaClausState,
    TurnOfMonthState,
    santa_claus_window,
    turn_of_month_window,
)
from stockscan.data.store import get_bars
from stockscan.db import session_scope

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

log = logging.getLogger(__name__)


# How many years of SPY history to pull for the seasonality / drawdown
# computations. 25 years is enough for monthly seasonality to stabilize
# (n=25 per month) and for the days-since-correction baseline to span
# multiple market cycles (2000, 2008, 2020).
_SPY_LOOKBACK_YEARS = 25


@dataclass(frozen=True, slots=True)
class CalendarState:
    """One bundle for the dashboard's "Calendar & Cycles" card.

    All fields are always populated — sub-states carry their own
    ``available`` flag when the underlying calc couldn't run.
    """

    as_of: _date
    monthly_seasonality: MonthlySeasonalityState
    halloween: HalloweenState
    presidential: PresidentialCycleState
    drawdown: DrawdownState
    turn_of_month: TurnOfMonthState
    santa_claus: SantaClausState
    january_barometer: JanuaryBarometerState
    decennial: DecennialState
    breadth: BreadthState
    # Diagnostic — list of indicator names that hard-failed (not just
    # produced ``available=False`` from missing data, but raised an
    # actual exception). Surfaced in a small footer chip so silent
    # breakage during a refactor is visible.
    failures: list[str] = field(default_factory=list)


def compute_calendar_state(
    as_of: _date | None = None,
    *,
    session: Session | None = None,
) -> CalendarState:
    """Compute every indicator for the dashboard's Calendar & Cycles card.

    Soft-fails per indicator. Always returns a fully-populated
    :class:`CalendarState`; broken indicators just have ``available=False``.

    Parameters
    ----------
    as_of:
        Date the state is computed for. Default = today. Useful to
        override in backtests.
    session:
        Optional caller-managed DB session. When ``None``, the
        orchestrator opens its own.
    """
    if as_of is None:
        as_of = _date.today()

    if session is None:
        with session_scope() as s:
            return _compute(as_of, s)
    return _compute(as_of, session)


def _compute(as_of: _date, session: Session) -> CalendarState:
    failures: list[str] = []

    # ---- Pull SPY bars once for every SPY-derived indicator. ----
    try:
        spy_start = as_of.replace(year=as_of.year - _SPY_LOOKBACK_YEARS)
    except ValueError:
        # Feb 29 on a non-leap year. Walk back one day.
        spy_start = (
            as_of.replace(year=as_of.year - _SPY_LOOKBACK_YEARS, month=2, day=28)
        )
    try:
        spy_bars = get_bars("SPY", spy_start, as_of, session=session)
    except Exception as exc:
        log.warning("calendar: SPY bars fetch failed: %s", exc)
        spy_bars = None
        failures.append("spy_bars")

    # ---- Tier 1: data-driven seasonality ----
    monthly = _safe(
        failures, "monthly_seasonality",
        lambda: monthly_seasonality(spy_bars, as_of),
        MonthlySeasonalityState.unavailable(as_of),
    )
    hween = _safe(
        failures, "halloween",
        lambda: halloween_window_stats(spy_bars, as_of),
        HalloweenState.unavailable(as_of),
    )
    pres = _safe(
        failures, "presidential",
        lambda: presidential_cycle_state(as_of),
        PresidentialCycleState.unavailable(as_of),
    )
    dd = _safe(
        failures, "drawdown",
        lambda: compute_drawdown_state(spy_bars, as_of),
        DrawdownState.unavailable(),
    )

    # ---- Tier 2 ----
    tom = _safe(
        failures, "turn_of_month",
        lambda: turn_of_month_window(as_of),
        TurnOfMonthState.unavailable(),
    )
    santa = _safe(
        failures, "santa_claus",
        lambda: santa_claus_window(as_of),
        SantaClausState.unavailable(),
    )
    jan_b = _safe(
        failures, "january_barometer",
        lambda: january_barometer(spy_bars, as_of),
        JanuaryBarometerState.unavailable(as_of),
    )
    dec = _safe(
        failures, "decennial",
        lambda: decennial_state(as_of),
        DecennialState.unavailable(as_of),
    )
    breadth = _safe(
        failures, "breadth",
        lambda: compute_breadth(session, as_of),
        BreadthState.unavailable(),
    )

    return CalendarState(
        as_of=as_of,
        monthly_seasonality=monthly,
        halloween=hween,
        presidential=pres,
        drawdown=dd,
        turn_of_month=tom,
        santa_claus=santa,
        january_barometer=jan_b,
        decennial=dec,
        breadth=breadth,
        failures=failures,
    )


def _safe(failures: list[str], name: str, fn, fallback):
    """Run ``fn``; on any exception, log + record + return ``fallback``."""
    try:
        return fn()
    except Exception as exc:
        log.warning("calendar/%s: failed: %s", name, exc)
        failures.append(name)
        return fallback
