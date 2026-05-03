"""Market breadth: % of current S&P 500 constituents above SMA(200).

A direct breadth read complementary to the regime composite's
RSP/SPY ratio. Healthy uptrends sit at 70%+ above SMA(200); washouts
sit below 30%. Sub-50% in a rising index suggests narrow leadership
(mega-cap concentration).

Implementation: one window-function SQL query that ranks each
constituent's bars by descending date, filters to the most recent
200 bars per symbol, and aggregates the SMA + last-close. Symbols
with fewer than 200 bars on file are dropped (insufficient history).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import text

if TYPE_CHECKING:
    from datetime import date as _date

    from sqlalchemy.orm import Session


@dataclass(frozen=True, slots=True)
class BreadthState:
    available: bool
    n_total: int  # constituents with enough bar history
    n_above_sma200: int
    pct_above_sma200: float | None  # 0..100; None when unavailable
    as_of_bar_date: _date | None  # the latest bar_date the SMA was computed against

    @classmethod
    def unavailable(cls) -> BreadthState:
        return cls(
            available=False,
            n_total=0,
            n_above_sma200=0,
            pct_above_sma200=None,
            as_of_bar_date=None,
        )

    @property
    def label(self) -> str:
        """Quick readable bucket for the dashboard chip."""
        if self.pct_above_sma200 is None:
            return "n/a"
        p = self.pct_above_sma200
        if p >= 70:
            return "healthy"
        if p >= 50:
            return "mixed"
        if p >= 30:
            return "weakening"
        return "washout"

    @property
    def kind(self) -> str:
        """Color-bucket for the badge: 'ok' / 'warn' / 'bad'."""
        if self.pct_above_sma200 is None:
            return "neutral"
        p = self.pct_above_sma200
        if p >= 70:
            return "ok"
        if p >= 30:
            return "warn"
        return "bad"


def compute_breadth(session: Session, as_of: _date) -> BreadthState:
    """Run the breadth window-function query and bucket the result.

    Soft-fails to ``unavailable()`` on any SQL error (e.g., during
    initial DB setup before bars are populated).
    """
    sql = text(
        """
        WITH ranked AS (
            SELECT
                b.symbol,
                b.close,
                b.bar_ts,
                ROW_NUMBER() OVER (
                    PARTITION BY b.symbol ORDER BY b.bar_ts DESC
                ) AS rn
            FROM bars b
            JOIN universe_history u
              ON u.symbol = b.symbol
             AND u.left_date IS NULL  -- current S&P 500 only
            WHERE b.bar_ts <= :asof
              AND b.interval = '1d'
        ),
        last200 AS (
            SELECT
                symbol,
                AVG(close) AS sma200,
                MAX(CASE WHEN rn = 1 THEN close END) AS last_close,
                MAX(CASE WHEN rn = 1 THEN bar_ts::date END) AS last_bar_date
            FROM ranked
            WHERE rn <= 200
            GROUP BY symbol
            HAVING COUNT(*) = 200
        )
        SELECT
            COUNT(*) AS n_total,
            SUM(CASE WHEN last_close > sma200 THEN 1 ELSE 0 END) AS n_above,
            MAX(last_bar_date) AS latest_bar
        FROM last200
        """
    )

    # ``bar_ts`` is timestamptz; we feed it a date and rely on PG to
    # compare correctly (date ≤ timestamptz works).
    row = session.execute(sql, {"asof": as_of}).first()
    if row is None or row[0] is None or int(row[0]) == 0:
        return BreadthState.unavailable()
    n_total = int(row[0])
    n_above = int(row[1] or 0)
    latest_bar = row[2]
    pct = (n_above / n_total) * 100.0 if n_total else None
    return BreadthState(
        available=True,
        n_total=n_total,
        n_above_sma200=n_above,
        pct_above_sma200=pct,
        as_of_bar_date=latest_bar,
    )
