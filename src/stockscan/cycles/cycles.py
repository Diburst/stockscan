"""Long-cycle indicators with hardcoded reference values.

Two indicators here — presidential election cycle and decennial
cycle — both depend on samples we cannot recompute from local SPY
bars (we'd need data going back to 1928 / 1881 respectively). We use
hardcoded historical averages from Hirsch's *Stock Trader's Almanac*.

The reference tables are stable — these are textbook values that have
been republished annually for decades. They don't drift unless the
Almanac re-baselines with a new computation methodology.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import date as _date

# ---------------------------------------------------------------------------
# Presidential cycle
# ---------------------------------------------------------------------------

# Year-of-cycle averages since 1928 (Stock Trader's Almanac, retold by
# numerous practitioners — Bankrate, CFA Institute, Quantified Strategies).
# Year 1 = year after election; Year 4 = election year itself.
# (avg_return_pct, positivity_rate)
_PRES_CYCLE_TABLE: dict[int, tuple[float, float]] = {
    1: (7.9, 0.58),
    2: (4.6, 0.58),
    3: (17.2, 0.78),  # historically the strongest year
    4: (7.3, 0.83),
}

# Election years define the anchor. 2024 was an election year, so:
#   2024 → year 4
#   2025 → year 1
#   2026 → year 2
#   2027 → year 3
# We hardcode 2025 as a known "year 1" anchor to make the offset
# math obvious and to avoid having the math break if/when election
# scheduling changes (constitutional amendments, etc).
_PRES_CYCLE_ANCHOR_YEAR = 2025
_PRES_CYCLE_ANCHOR_YEAR_OF_CYCLE = 1


@dataclass(frozen=True, slots=True)
class PresidentialCycleState:
    available: bool
    year: int
    year_of_cycle: int  # 1..4
    label: str  # "post-election" / "midterm" / "pre-election" / "election"
    historical_avg_pct: float | None
    historical_positive_rate: float | None
    is_strongest_year: bool  # True only when year_of_cycle == 3

    @classmethod
    def unavailable(cls, as_of: _date) -> PresidentialCycleState:
        return cls(
            available=False,
            year=as_of.year,
            year_of_cycle=0,
            label="?",
            historical_avg_pct=None,
            historical_positive_rate=None,
            is_strongest_year=False,
        )


def presidential_cycle_state(as_of: _date) -> PresidentialCycleState:
    """Map ``as_of.year`` to its position in the 4-year cycle."""
    offset = (as_of.year - _PRES_CYCLE_ANCHOR_YEAR) % 4
    year_of_cycle = ((_PRES_CYCLE_ANCHOR_YEAR_OF_CYCLE - 1 + offset) % 4) + 1
    avg, pos = _PRES_CYCLE_TABLE[year_of_cycle]
    return PresidentialCycleState(
        available=True,
        year=as_of.year,
        year_of_cycle=year_of_cycle,
        label={
            1: "post-election",
            2: "midterm",
            3: "pre-election",
            4: "election",
        }[year_of_cycle],
        historical_avg_pct=avg,
        historical_positive_rate=pos,
        is_strongest_year=(year_of_cycle == 3),
    )


# ---------------------------------------------------------------------------
# Decennial cycle
# ---------------------------------------------------------------------------

# Year-ending-digit averages since 1881 (Stock Trader's Almanac).
# Famous pattern: years ending in 5 dramatically outperform; years
# ending in 0 underperform. Sample size is small (one observation per
# decade) so the practical edge is dubious — included as trivia.
_DECENNIAL_TABLE: dict[int, float] = {
    0: -1.6,
    1: 5.5,
    2: 6.4,
    3: 9.1,
    4: -0.3,
    5: 28.4,  # by far the strongest digit
    6: 6.5,
    7: 1.6,
    8: 12.3,
    9: 13.7,
}


@dataclass(frozen=True, slots=True)
class DecennialState:
    available: bool
    year: int
    year_ending_digit: int
    historical_avg_pct: float | None
    is_strongest_digit: bool  # True only for years ending in 5

    @classmethod
    def unavailable(cls, as_of: _date) -> DecennialState:
        return cls(
            available=False,
            year=as_of.year,
            year_ending_digit=as_of.year % 10,
            historical_avg_pct=None,
            is_strongest_digit=False,
        )


def decennial_state(as_of: _date) -> DecennialState:
    digit = as_of.year % 10
    return DecennialState(
        available=True,
        year=as_of.year,
        year_ending_digit=digit,
        historical_avg_pct=_DECENNIAL_TABLE[digit],
        is_strongest_digit=(digit == 5),
    )
