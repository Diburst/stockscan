"""Insider transaction refresh with 23-hour cooldown enforcement.

Each upstream call to ``/api/insider-transactions`` costs **10 API
credits**. The cooldown gate is mandatory at every entry point — never
call the provider without first checking ``can_refresh(scope)``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

from stockscan.insider.cooldown import (
    REFRESH_COOLDOWN_HOURS,
    can_refresh,
    finish_refresh,
    start_refresh,
)
from stockscan.insider.store import upsert_transactions

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from stockscan.data.providers.base import DataProvider

log = logging.getLogger(__name__)


# Insider-transaction lookback for the per-symbol pull. 90 days covers
# the canonical "trailing-quarter" window used in academic literature
# without ballooning the response size.
_DEFAULT_LOOKBACK_DAYS = 90


@dataclass(frozen=True, slots=True)
class InsiderRefreshResult:
    """Outcome of one refresh attempt — success, skipped (cooldown), or error."""

    scope: str
    skipped: bool                       # True when blocked by cooldown
    cooldown_remaining_secs: float | None
    symbols_refreshed: int
    transactions_upserted: int
    started_at: datetime
    finished_at: datetime
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return not self.skipped and self.error is None

    @property
    def duration_seconds(self) -> float:
        return (self.finished_at - self.started_at).total_seconds()


def _pull_for_symbol(
    provider: DataProvider,
    symbol: str,
    *,
    lookback_days: int,
    session: Session | None,
) -> int:
    """Single-symbol fetch + upsert. Soft-fails per symbol."""
    if not hasattr(provider, "get_insider_transactions"):
        log.warning(
            "_pull_for_symbol: provider %r missing get_insider_transactions",
            type(provider).__name__,
        )
        return 0
    today = date.today()
    start = today - timedelta(days=lookback_days)
    try:
        records = provider.get_insider_transactions(  # type: ignore[attr-defined]
            symbol=symbol, start=start, end=today, limit=1000,
        )
    except Exception as exc:
        log.warning("_pull_for_symbol[%s] provider call failed: %s", symbol, exc)
        return 0
    # IMPORTANT: pass ``symbol=`` so the upsert layer can attribute the
    # records even if the API response omits a per-record ticker (which
    # it sometimes does when the query was scoped via the ``code=``
    # parameter — the API doesn't always echo the symbol back). Without
    # this fallback every record was getting silently dropped.
    return upsert_transactions(records, symbol=symbol, session=session)


def refresh_insider_for_watchlist(
    provider: DataProvider,
    symbols: list[str],
    *,
    lookback_days: int = _DEFAULT_LOOKBACK_DAYS,
    session: Session | None = None,
    cooldown_hours: float = REFRESH_COOLDOWN_HOURS,
) -> InsiderRefreshResult:
    """Refresh insider data for every watchlist symbol, gated by 23h cooldown.

    If the previous successful refresh was within the cooldown window,
    returns immediately with ``skipped=True`` — the caller can render a
    "Just refreshed; try again in Nh" toast or simply ignore it.

    Each symbol is one provider call (10 credits). A 30-symbol watchlist
    costs ~300 credits per successful refresh, run at most once a day.
    """
    scope = "watchlist"
    started = datetime.now(UTC)
    allowed, remaining = can_refresh(
        scope, cooldown_hours=cooldown_hours, session=session,
    )
    if not allowed:
        log.info(
            "insider refresh skipped: %.1fh cooldown remaining for %s",
            (remaining or 0) / 3600.0, scope,
        )
        return InsiderRefreshResult(
            scope=scope,
            skipped=True,
            cooldown_remaining_secs=remaining,
            symbols_refreshed=0,
            transactions_upserted=0,
            started_at=started,
            finished_at=datetime.now(UTC),
        )

    refresh_id = start_refresh(scope, session=session)
    total_upserted = 0
    n_symbols = 0
    error: str | None = None
    try:
        for sym in symbols:
            total_upserted += _pull_for_symbol(
                provider, sym,
                lookback_days=lookback_days, session=session,
            )
            n_symbols += 1
    except Exception as exc:
        log.exception("refresh_insider_for_watchlist: unexpected error")
        error = str(exc)

    finished = datetime.now(UTC)
    finish_refresh(
        refresh_id,
        success=error is None,
        symbols_refreshed=n_symbols,
        transactions_upserted=total_upserted,
        error_message=error,
        session=session,
    )
    return InsiderRefreshResult(
        scope=scope,
        skipped=False,
        cooldown_remaining_secs=None,
        symbols_refreshed=n_symbols,
        transactions_upserted=total_upserted,
        started_at=started,
        finished_at=finished,
        error=error,
    )


def refresh_insider_for_symbol(
    provider: DataProvider,
    symbol: str,
    *,
    lookback_days: int = _DEFAULT_LOOKBACK_DAYS,
    session: Session | None = None,
    cooldown_hours: float = REFRESH_COOLDOWN_HOURS,
) -> InsiderRefreshResult:
    """On-demand single-symbol refresh from the analysis page.

    Per-symbol cooldown — distinct from the watchlist-wide cooldown so
    clicking "Refresh insider" on one symbol doesn't block a separate
    watchlist refresh later.
    """
    scope = f"symbol:{symbol}"
    started = datetime.now(UTC)
    allowed, remaining = can_refresh(
        scope, cooldown_hours=cooldown_hours, session=session,
    )
    if not allowed:
        return InsiderRefreshResult(
            scope=scope,
            skipped=True,
            cooldown_remaining_secs=remaining,
            symbols_refreshed=0,
            transactions_upserted=0,
            started_at=started,
            finished_at=datetime.now(UTC),
        )
    refresh_id = start_refresh(scope, session=session)
    error: str | None = None
    upserted = 0
    try:
        upserted = _pull_for_symbol(
            provider, symbol,
            lookback_days=lookback_days, session=session,
        )
    except Exception as exc:
        log.exception("refresh_insider_for_symbol[%s] unexpected error", symbol)
        error = str(exc)

    finished = datetime.now(UTC)
    finish_refresh(
        refresh_id,
        success=error is None,
        symbols_refreshed=1,
        transactions_upserted=upserted,
        error_message=error,
        session=session,
    )
    return InsiderRefreshResult(
        scope=scope,
        skipped=False,
        cooldown_remaining_secs=None,
        symbols_refreshed=1,
        transactions_upserted=upserted,
        started_at=started,
        finished_at=finished,
        error=error,
    )
