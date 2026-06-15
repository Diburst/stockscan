"""Bulk historical backfill from a DataProvider into the local store.

Used by `stockscan refresh --full` on first run, and for one-off backfills
when adding a new symbol or extending history.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Iterable
from zoneinfo import ZoneInfo

from stockscan.data.providers.base import DataProvider
from stockscan.data.store import latest_bar_date, upsert_bars

log = logging.getLogger(__name__)

_NY_TZ = ZoneInfo("America/New_York")

# When a trading day's EOD bars become safe to fetch. EODHD posts NYSE/NASDAQ
# end-of-day data ~15 min after the 16:00 ET close; we wait until 18:00 ET (a
# ~2h margin) so a refresh never spends an API credit on a not-yet-posted day.
# Tunable if you want fresher same-day data (lower) or more safety (higher).
EOD_AVAILABLE_HOUR_ET = 18


def latest_completed_session(now: datetime | None = None) -> date:
    """The most recent trading session whose EOD bars should be available.

    Today counts only after ``EOD_AVAILABLE_HOUR_ET`` ET on a weekday;
    otherwise we fall back to the most recent prior weekday. Weekend/After-
    hours refreshes therefore target the last closed session, so a store that
    already has it is a true zero-cost no-op (no day to fetch).

    Holidays use the weekday proxy the rest of the data layer uses — on a
    market holiday the bulk endpoint simply returns empty, same as before.
    """
    et = (now or datetime.now(timezone.utc)).astimezone(_NY_TZ)
    d = et.date()
    if not (d.weekday() < 5 and et.hour >= EOD_AVAILABLE_HOUR_ET):
        d = d - timedelta(days=1)
    while d.weekday() >= 5:  # Sat/Sun → step back to Friday
        d = d - timedelta(days=1)
    return d


def backfill_symbol(
    provider: DataProvider,
    symbol: str,
    *,
    start: date,
    end: date | None = None,
    overlap_days: int = 5,
    exchange: str = "US",
) -> int:
    """Backfill `symbol` from provider into local store.

    If we already have data for this symbol, only fetch from
    `latest_bar_date - overlap_days` (so corrections to the last few bars
    are picked up). Otherwise fetch the full window from `start`.

    `exchange` is forwarded to the provider's `get_bars` to select the
    EODHD-style suffix (e.g., ``"US"`` for equities, ``"INDX"`` for cash
    indices like VIX).
    """
    end = end or date.today()
    last = latest_bar_date(symbol)
    fetch_start = max(start, last - timedelta(days=overlap_days)) if last else start
    if fetch_start > end:
        log.debug("backfill: %s already up to date (last=%s)", symbol, last)
        return 0
    log.info("backfill: %s.%s [%s..%s]", symbol, exchange, fetch_start, end)
    rows = provider.get_bars(symbol, fetch_start, end, exchange=exchange)
    return upsert_bars(rows)


def backfill_universe(
    provider: DataProvider,
    symbols: Iterable[str],
    *,
    start: date,
    end: date | None = None,
    exchange: str = "US",
) -> dict[str, int]:
    """Backfill many symbols sequentially. Returns per-symbol upsert counts."""
    out: dict[str, int] = {}
    for s in symbols:
        try:
            out[s] = backfill_symbol(provider, s, start=start, end=end, exchange=exchange)
        except Exception as exc:  # noqa: BLE001
            log.error("backfill failed for %s: %s", s, exc)
            out[s] = -1
    return out


# -----------------------------------------------------------------------
# Bulk daily refresh path (Phase 3)
# -----------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class BulkRefreshResult:
    """Outcome of a multi-day bulk refresh, including which days failed.

    Supports ``int(result)`` / ``+`` so callers that only care about the
    upsert count keep working unchanged; callers that report status (the
    nightly summary, Fetch Latest) read ``failed_days``.
    """

    upserted: int
    failed_days: tuple[date, ...] = ()

    def __int__(self) -> int:
        return self.upserted

    def __radd__(self, other: int) -> int:
        return other + self.upserted

    @property
    def ok(self) -> bool:
        return not self.failed_days


def refresh_recent_days_bulk(
    provider: DataProvider,
    dates: Iterable[date],
    *,
    exchange: str = "US",
    filter_to: set[str] | None = None,
) -> BulkRefreshResult:
    """Use the bulk endpoint to fetch one trading day at a time for ALL
    symbols on `exchange`, then upsert.

    For our daily refresh job this is dramatically more efficient than
    per-symbol fetches: one API call per day instead of one per (symbol, day).
    A daily nightly run is one call. A 5-day catch-up after an outage is
    five calls.

    `filter_to` restricts the upsert to a known symbol set (e.g., the
    historical S&P 500 universe). Symbols outside the filter are dropped
    rather than persisted.

    One bad day never aborts the rest — it's recorded in
    ``BulkRefreshResult.failed_days`` so the caller can surface partial
    success ("refreshed 2 of 3 days") instead of silently under-reporting.
    """
    from stockscan.data.store import upsert_bars

    total = 0
    failed: list[date] = []
    for d in dates:
        try:
            rows = provider.get_eod_bulk(d, exchange=exchange)
        except Exception as exc:  # noqa: BLE001
            log.error("bulk fetch failed for %s: %s", d, exc)
            failed.append(d)
            continue
        if filter_to is not None:
            rows = [r for r in rows if r.symbol in filter_to]
        if not rows:
            log.info("bulk %s: empty (likely a holiday)", d)
            continue
        n = upsert_bars(rows)
        log.info("bulk %s: %d bars upserted", d, n)
        total += n
    return BulkRefreshResult(upserted=total, failed_days=tuple(failed))


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
