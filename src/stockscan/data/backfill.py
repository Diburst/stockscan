"""Bulk historical backfill from a DataProvider into the local store.

Used by `stockscan refresh --full` on first run, and for one-off backfills
when adding a new symbol or extending history.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Iterable

from stockscan.data.providers.base import DataProvider
from stockscan.data.store import latest_bar_date, upsert_bars

log = logging.getLogger(__name__)


def backfill_symbol(
    provider: DataProvider,
    symbol: str,
    *,
    start: date,
    end: date | None = None,
    overlap_days: int = 5,
) -> int:
    """Backfill `symbol` from provider into local store.

    If we already have data for this symbol, only fetch from
    `latest_bar_date - overlap_days` (so corrections to the last few bars
    are picked up). Otherwise fetch the full window from `start`.
    """
    end = end or date.today()
    last = latest_bar_date(symbol)
    fetch_start = max(start, last - timedelta(days=overlap_days)) if last else start
    if fetch_start > end:
        log.debug("backfill: %s already up to date (last=%s)", symbol, last)
        return 0
    log.info("backfill: %s [%s..%s]", symbol, fetch_start, end)
    rows = provider.get_bars(symbol, fetch_start, end)
    return upsert_bars(rows)


def backfill_universe(
    provider: DataProvider,
    symbols: Iterable[str],
    *,
    start: date,
    end: date | None = None,
) -> dict[str, int]:
    """Backfill many symbols sequentially. Returns per-symbol upsert counts."""
    out: dict[str, int] = {}
    for s in symbols:
        try:
            out[s] = backfill_symbol(provider, s, start=start, end=end)
        except Exception as exc:  # noqa: BLE001
            log.error("backfill failed for %s: %s", s, exc)
            out[s] = -1
    return out


# -----------------------------------------------------------------------
# Bulk daily refresh path (Phase 3)
# -----------------------------------------------------------------------
def refresh_recent_days_bulk(
    provider: DataProvider,
    dates: Iterable[date],
    *,
    exchange: str = "US",
    filter_to: set[str] | None = None,
) -> int:
    """Use the bulk endpoint to fetch one trading day at a time for ALL
    symbols on `exchange`, then upsert.

    For our daily refresh job this is dramatically more efficient than
    per-symbol fetches: one API call per day instead of one per (symbol, day).
    A daily nightly run is one call. A 5-day catch-up after an outage is
    five calls.

    `filter_to` restricts the upsert to a known symbol set (e.g., the
    historical S&P 500 universe). Symbols outside the filter are dropped
    rather than persisted.
    """
    from stockscan.data.store import upsert_bars

    total = 0
    for d in dates:
        try:
            rows = provider.get_eod_bulk(d, exchange=exchange)
        except Exception as exc:  # noqa: BLE001
            log.error("bulk fetch failed for %s: %s", d, exc)
            continue
        if filter_to is not None:
            rows = [r for r in rows if r.symbol in filter_to]
        if not rows:
            log.info("bulk %s: empty (likely a holiday)", d)
            continue
        n = upsert_bars(rows)
        log.info("bulk %s: %d bars upserted", d, n)
        total += n
    return total


def trading_days_since(last_date: date | None, until: date) -> list[date]:
    """Approximate set of trading days strictly after `last_date` up to `until`.

    We don't have a market-calendar lib, so we use weekdays as a proxy.
    The bulk endpoint silently skips holidays (returns no rows for them),
    so a few extra calls for non-trading-day requests is harmless.
    """
    if last_date is None:
        last_date = until - timedelta(days=10)
    out: list[date] = []
    d = last_date + timedelta(days=1)
    while d <= until:
        if d.weekday() < 5:  # Mon=0..Fri=4
            out.append(d)
        d += timedelta(days=1)
    return out
