"""Scan refresh orchestration — the read-out from the 'Fetch latest' button.

Two phases per call:

  1. **Bars catch-up** — pull the last ``days_back`` trading days of
     bars for the active universe via the EODHD bulk endpoint
     (``/eod-bulk-last-day/{exchange}``). One API call per day, NOT one
     per (symbol, day), so a 7-day catch-up after a missed nightly job
     is 7 calls — cheap enough to wire to a button without rate-limit
     anxiety. Symbols outside the current S&P 500 are filtered out at
     upsert time so we don't drift into the long tail.

  2. **Strategy fan-out** — for each registered strategy, run the
     scanner at ``as_of=today`` and persist the resulting passing /
     rejected signals.

Soft-fails per strategy: a single bad strategy logs a warning and the
others still get to run. The HTTP route surfaces aggregate counts +
the per-failure list as a small banner at the top of the signals page.

Mirrors :mod:`stockscan.news.refresh` in shape so the two refresh
endpoints look the same to the route layer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

from stockscan.data.backfill import refresh_recent_days_bulk, trading_days_since
from stockscan.scan.runner import ScanRunner, ScanSummary
from stockscan.strategies import STRATEGY_REGISTRY, discover_strategies
from stockscan.universe import current_constituents

if TYPE_CHECKING:
    from collections.abc import Iterable

    from sqlalchemy.orm import Session

    from stockscan.data.providers.base import DataProvider

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class StrategyRunFailure:
    """One strategy's run that errored out — surfaced in the UI banner."""

    strategy_name: str
    error: str


@dataclass(frozen=True, slots=True)
class SignalsRefreshResult:
    """Summary of one ``refresh_signals`` invocation.

    Surfaced to the user as the green/red feedback chip on the Signals
    page and logged at INFO for the daily cron path.
    """

    bars_upserted: int  # rows upserted across all bulk-EOD calls
    bars_days_covered: int  # actual trading days the bulk loop iterated
    strategies_run: int  # strategies whose scan completed (success or empty)
    signals_emitted: int  # sum of passing across all strategies
    rejected_count: int  # sum of rejected across all strategies
    failures: list[StrategyRunFailure] = field(default_factory=list)
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def duration_seconds(self) -> float:
        return (self.finished_at - self.started_at).total_seconds()


def _bulk_dates(days_back: int) -> list[date]:
    """Trading days (weekdays) within the trailing ``days_back`` window.

    Uses :func:`trading_days_since` from the data layer so the holiday-
    handling behaviour stays consistent with the nightly refresh job.
    The bulk endpoint silently returns empty for non-trading days, so a
    handful of unnecessary calls (e.g., over a long weekend) is harmless.
    """
    today = date.today()
    last_date = today - timedelta(days=days_back)
    return trading_days_since(last_date, today)


def refresh_signals(
    provider: DataProvider,
    *,
    days_back: int = 7,
    only_strategies: Iterable[str] | None = None,
    session: Session | None = None,
) -> SignalsRefreshResult:
    """Backfill recent bars + run all strategies. Returns a tidy summary.

    Parameters
    ----------
    provider:
        DataProvider with both ``get_eod_bulk`` (bars) and ``get_bars``
        capability — i.e., :class:`EODHDProvider` in production.
    days_back:
        How many calendar days of bars to backfill. Default 7 covers a
        long weekend + a few weekdays of catch-up. The bulk endpoint
        will skip non-trading days at zero cost.
    only_strategies:
        If supplied, restricts the strategy fan-out to these names.
        Default ``None`` = run every registered strategy.
    session:
        Optional caller-managed DB session. Always reuse the request's
        session inside a FastAPI route so the bars upsert + strategy
        runs sit in one transaction and roll back together on error.

    Returns
    -------
    SignalsRefreshResult
        Aggregate counts; ``failures`` lists per-strategy errors that
        didn't kill the whole refresh.
    """
    started = datetime.now(UTC)
    discover_strategies()

    # ------------------------------------------------------------------
    # Phase 1: Bars catch-up via the bulk EOD endpoint.
    # ------------------------------------------------------------------
    universe = set(current_constituents(session=session))
    dates = _bulk_dates(days_back)
    bars_upserted = 0
    if dates:
        try:
            bars_upserted = refresh_recent_days_bulk(
                provider,
                dates,
                exchange="US",
                filter_to=universe or None,
            )
        except Exception as exc:  # provider hard-down; continue to scans anyway
            log.exception("scan refresh: bulk bars fetch failed: %s", exc)
    else:
        log.info("scan refresh: no trading days in window (days_back=%d)", days_back)

    # ------------------------------------------------------------------
    # Phase 2: Strategy fan-out.
    # ------------------------------------------------------------------
    targets: list[str] = (
        list(only_strategies)
        if only_strategies is not None
        else STRATEGY_REGISTRY.names()
    )

    runner = ScanRunner(session=session)
    today = date.today()
    failures: list[StrategyRunFailure] = []
    summaries: list[ScanSummary] = []
    for name in targets:
        try:
            summaries.append(runner.run(name, today))
        except Exception as exc:  # Soft-fail per strategy; keep going.
            log.exception("scan refresh: strategy %s failed", name)
            failures.append(StrategyRunFailure(strategy_name=name, error=str(exc)))

    finished = datetime.now(UTC)
    result = SignalsRefreshResult(
        bars_upserted=bars_upserted,
        bars_days_covered=len(dates),
        strategies_run=len(summaries),
        signals_emitted=sum(s.signals_emitted for s in summaries),
        rejected_count=sum(s.rejected_count for s in summaries),
        failures=failures,
        started_at=started,
        finished_at=finished,
    )
    log.info(
        "scan refresh: %d bars across %d days, %d strategies → %d signals "
        "(%d rejected, %d failed, took %.1fs)",
        result.bars_upserted,
        result.bars_days_covered,
        result.strategies_run,
        result.signals_emitted,
        result.rejected_count,
        len(result.failures),
        result.duration_seconds,
    )
    return result
