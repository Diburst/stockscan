"""Calendar / cycle context indicators for the dashboard.

Nine indicators arranged in two tiers per the design discussion:

  Tier 1 (robust, multi-decade evidence):
    * Monthly seasonality
    * Halloween window ("Sell in May")
    * Presidential election cycle
    * SPY drawdown + days since last correction

  Tier 2 (smaller / noisier but still informative):
    * Turn-of-month effect
    * Santa Claus rally window
    * January Barometer
    * Decennial cycle
    * Breadth: % of S&P 500 above SMA(200)

All Tier 1 'live' stats are computed from local SPY bars (so the
window matches whatever history you have stored). Two indicators
that need history we'll never have locally — the presidential and
decennial cycles, which want 50-100 years of data — use hardcoded
reference values from Hirsch's *Stock Trader's Almanac*.

Every sub-indicator soft-fails with ``available=False`` when its
inputs are missing, so a partial bar history doesn't blank out the
whole card.
"""

from __future__ import annotations

from stockscan.cycles.state import (
    CalendarState,
    compute_calendar_state,
)

__all__ = [
    "CalendarState",
    "compute_calendar_state",
]
