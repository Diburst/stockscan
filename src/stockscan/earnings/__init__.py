"""Earnings: report calendar (existing) + forward estimate trends (new).

Two distinct datasets:

  * **earnings_calendar** (migration 0001) — when a company reports + the
    consensus EPS estimate and post-release actual + time-of-day (BMO /
    AMC / unknown). Already in the project; this package adds the higher-
    level ``refresh_earnings_calendar`` helper that pulls upcoming earnings
    for a symbol set.

  * **earnings_trends** (migration 0018) — forward analyst expectations
    over multiple horizons (0q, +1q, 0y, +1y) with the historical
    consensus trajectory (current / 7d / 30d / 60d / 90d snapshots) and
    revision counts (up vs down in last 7 / 30 days). This is the
    estimate-revision drift signal — well-studied source of post-
    announcement / pre-announcement returns.
"""

from __future__ import annotations

from stockscan.earnings.calendar_store import (
    EarningsEntry,
    days_until,
    earnings_in_window,
    next_earnings,
    upsert_earnings,
)
from stockscan.earnings.refresh import (
    EarningsRefreshResult,
    refresh_earnings,
    refresh_earnings_calendar,
    refresh_earnings_trends,
)
from stockscan.earnings.trends_store import (
    EarningsTrend,
    latest_trend,
    revision_summary,
    upsert_trends,
)

__all__ = [
    "EarningsEntry",
    "EarningsRefreshResult",
    "EarningsTrend",
    "days_until",
    "earnings_in_window",
    "latest_trend",
    "next_earnings",
    "refresh_earnings",
    "refresh_earnings_calendar",
    "refresh_earnings_trends",
    "revision_summary",
    "upsert_earnings",
    "upsert_trends",
]
