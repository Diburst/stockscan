"""S&P 500 universe — live + historical membership.

Reads from `universe_history`. Refresh from a DataProvider with `refresh_universe`.

Critical for survivorship-bias-free backtesting: `members_as_of(d)` returns
exactly the set of symbols that were in the index on date `d`.
"""

from __future__ import annotations

import logging
from datetime import date

from sqlalchemy import text
from sqlalchemy.orm import Session

from stockscan.data.providers.base import DataProvider, UniverseMember
from stockscan.db import session_scope

log = logging.getLogger(__name__)


def refresh_universe(provider: DataProvider, *, session: Session | None = None) -> int:
    """Pull historical + current S&P 500 membership and persist."""
    historical = provider.get_sp500_historical_constituents()
    current = provider.get_sp500_constituents()

    # Treat current as 'still a member': set left_date=NULL.
    by_key: dict[tuple[str, date], UniverseMember] = {}
    for m in historical:
        by_key[(m.symbol, m.joined_date)] = m
    for m in current:
        # Keep the existing joined_date if already present; otherwise insert.
        key = (m.symbol, m.joined_date)
        if key not in by_key:
            by_key[key] = m

    rows = list(by_key.values())
    if not rows:
        log.warning("refresh_universe: provider returned no constituents")
        return 0

    sql = text(
        """
        INSERT INTO universe_history (symbol, joined_date, left_date)
        VALUES (:symbol, :joined_date, :left_date)
        ON CONFLICT (symbol, joined_date) DO UPDATE SET
            left_date = EXCLUDED.left_date;
        """
    )
    payload = [
        {"symbol": m.symbol, "joined_date": m.joined_date, "left_date": m.left_date}
        for m in rows
    ]

    def _run(s: Session) -> int:
        s.execute(sql, payload)
        return len(payload)

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)


def current_constituents(*, session: Session | None = None) -> list[str]:
    """Symbols that are members of the index today (left_date IS NULL)."""
    sql = text(
        "SELECT DISTINCT symbol FROM universe_history WHERE left_date IS NULL ORDER BY symbol"
    )

    def _run(s: Session) -> list[str]:
        return [row[0] for row in s.execute(sql)]

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)


def all_known_symbols(*, session: Session | None = None) -> list[str]:
    """Every symbol that has ever been in the index (current + delisted + removed).

    This is the right symbol set for `refresh bars` — you want bars for the
    historical members too, otherwise a backtest of 2015 has no data for
    companies that have since been removed from the index, which silently
    re-introduces survivorship bias.
    """
    sql = text("SELECT DISTINCT symbol FROM universe_history ORDER BY symbol")

    def _run(s: Session) -> list[str]:
        return [row[0] for row in s.execute(sql)]

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)


def members_as_of(d: date, *, session: Session | None = None) -> list[str]:
    """Symbols in the index on `d`. Used to avoid survivorship bias in backtests."""
    sql = text(
        """
        SELECT DISTINCT symbol
        FROM universe_history
        WHERE joined_date <= :d
          AND (left_date IS NULL OR left_date > :d)
        ORDER BY symbol
        """
    )

    def _run(s: Session) -> list[str]:
        return [row[0] for row in s.execute(sql, {"d": d})]

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)
