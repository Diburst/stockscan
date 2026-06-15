"""Nightly job (DESIGN §4.9): refresh → scan → notify.

Steps in order:
  1. Bulk-refresh recent EOD bars for the historical S&P 500 universe.
  2. Run every registered strategy as of today.
  3. Send a summary notification (email + Discord, whichever is configured).

Designed to be safe to re-run within a day — refresh is idempotent, and
the scanner persists a new strategy_runs row each invocation. The summary
notification reports the totals from this latest run.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING

from stockscan.config import settings
from stockscan.data.backfill import (
    BulkRefreshResult,
    refresh_recent_days_bulk,
    trading_days_since,
)
from stockscan.data.providers import EODHDProvider, StubProvider
from stockscan.data.providers.base import DataProvider
from stockscan.data.store import latest_bar_date
from stockscan.notify import notify
from stockscan.regime import detect_regime
from stockscan.scan import ScanRunner, ScanSummary
from stockscan.strategies import STRATEGY_REGISTRY, discover_strategies
from stockscan.universe import all_known_symbols
from stockscan.watchlist import check_and_fire_alerts

if TYPE_CHECKING:
    from stockscan.regime.store import MarketRegime

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class NightlyResult:
    as_of: date
    bars_upserted: int
    scans: list[ScanSummary]
    watchlist_alerts_fired: int = 0
    # Step failures collected during the run ("step: error"). Carried into
    # the summary notification so an overnight failure reaches Discord/email
    # instead of dying quietly in a log file.
    step_failures: list[str] = field(default_factory=list)


def _provider() -> DataProvider:
    key = settings.eodhd_api_key.get_secret_value()
    return EODHDProvider(api_key=key) if key else StubProvider()


def run_nightly_scan(
    as_of: date | None = None,
    *,
    notify_channels=None,
) -> NightlyResult:
    """Run the full nightly flow. Returns counts for logging/reporting.

    Every step is individually fault-tolerant: a failure logs, is recorded
    in ``step_failures``, and the run continues. The failures list is
    appended to the summary notification so the operator hears about a
    degraded run on the same channel as a healthy one.
    """
    as_of = as_of or date.today()
    job_started = time.perf_counter()
    discover_strategies()
    failures: list[str] = []

    def _step_done(label: str, started: float) -> None:
        log.info("nightly: step '%s' done in %.1fs", label, time.perf_counter() - started)

    # 1. Refresh recent EOD bars via the bulk endpoint.
    t = time.perf_counter()
    bulk = _refresh_recent_bars(as_of)
    bars_upserted = bulk.upserted
    if bulk.failed_days:
        failures.append(
            "bars refresh: %d day(s) failed (%s)"
            % (len(bulk.failed_days), ", ".join(str(d) for d in bulk.failed_days))
        )
    _step_done("refresh bars", t)

    # 1b. Rebuild equal-weight sector composites from the freshly-refreshed bars,
    #     BEFORE the scans so sector_rs sees today's data. Full rebuild is
    #     idempotent and uses only local bars (no API calls); it can be swapped
    #     for an incremental append later if the read cost becomes an issue.
    t = time.perf_counter()
    try:
        from stockscan.sectors.store import DEFAULT_BASE_START, refresh_sector_composites

        composites = refresh_sector_composites(DEFAULT_BASE_START, as_of)
        log.info("nightly: rebuilt %d sector composites", len(composites))
    except Exception as exc:
        log.error("nightly: sector-composite rebuild failed: %s", exc)
        failures.append(f"sector composites: {exc}")
    _step_done("sector composites", t)

    # 2. Run every registered strategy.
    t = time.perf_counter()
    runner = ScanRunner()
    scans: list[ScanSummary] = []
    for name in STRATEGY_REGISTRY.names():
        try:
            scans.append(runner.run(name, as_of))
        except Exception as exc:
            log.error("scan %s failed: %s", name, exc)
            failures.append(f"scan {name}: {exc}")
    _step_done("strategy scans", t)

    # 3. Fire any triggered watchlist alerts (uses fresh bars from step 1).
    alerts_fired = 0
    try:
        result = check_and_fire_alerts(channels=notify_channels)
        alerts_fired = len(result.fired)
    except Exception as exc:
        log.error("watchlist alert check failed: %s", exc)
        failures.append(f"watchlist alerts: {exc}")

    # 4. Detect (and cache) today's regime — uses the freshly-refreshed bars.
    regime: MarketRegime | None = None
    try:
        regime = detect_regime(as_of)
    except Exception as exc:
        log.warning("nightly: regime detection failed: %s", exc)
        failures.append(f"regime detection: {exc}")

    # 5. Send a summary notification (includes any step failures).
    _send_summary(
        as_of,
        bars_upserted,
        scans,
        regime=regime,
        watchlist_alerts=alerts_fired,
        failures=failures,
        channels=notify_channels,
    )

    log.info(
        "nightly: run complete for %s — %d bars, %d scans, %d failures, %.1fs total",
        as_of,
        bars_upserted,
        len(scans),
        len(failures),
        time.perf_counter() - job_started,
    )

    return NightlyResult(
        as_of=as_of,
        bars_upserted=bars_upserted,
        scans=scans,
        watchlist_alerts_fired=alerts_fired,
        step_failures=failures,
    )


def _refresh_recent_bars(as_of: date) -> BulkRefreshResult:
    """Bulk-refresh days since our latest stored bar — typically 1 day."""
    # Use a representative symbol to find the latest stored bar.
    universe = all_known_symbols()
    if not universe:
        log.warning("nightly: universe is empty — run `stockscan refresh universe` first")
        return BulkRefreshResult(upserted=0)

    # Pick the symbol most likely to be present.
    sample = "SPY" if "SPY" in universe else (universe[0] if universe else None)
    last = latest_bar_date(sample) if sample else None
    days = trading_days_since(last, as_of)
    if not days:
        log.info("nightly: no missing trading days (last=%s, as_of=%s)", last, as_of)
        return BulkRefreshResult(upserted=0)

    log.info("nightly: bulk-refreshing %d days (%s..%s)", len(days), days[0], days[-1])
    p = _provider()
    try:
        return refresh_recent_days_bulk(p, days, filter_to=set(universe))
    finally:
        close = getattr(p, "close", None)
        if callable(close):
            close()


def _send_summary(
    as_of: date,
    bars_upserted: int,
    scans: list[ScanSummary],
    *,
    regime: MarketRegime | None = None,
    watchlist_alerts: int = 0,
    failures: list[str] | None = None,
    channels=None,
) -> None:
    regime_label = regime.regime.replace("_", " ") if regime else "unknown"
    failures = failures or []

    def _failure_block() -> list[str]:
        if not failures:
            return []
        return ["", "⚠ Step failures (run degraded):"] + [
            f"  ✗ {f}" for f in failures
        ]

    if not scans:
        body = (
            f"Nightly run for {as_of}\n\n"
            f"Market regime: {regime_label}\n"
            f"No strategies registered. Refreshed {bars_upserted} bars.\n"
            + "\n".join(_failure_block())
        )
        notify(f"stockscan · {as_of}", body, channels=channels)
        return

    # Under v2 soft sizing nothing is "paused" by regime — every strategy
    # runs and is sized by its ``regime_multiplier``. The legacy
    # ``regime_skipped`` partition is preserved as a back-compat fallback
    # for any v1 ScanSummary that still surfaces; in practice it's always
    # False going forward.
    active = [s for s in scans if not s.regime_skipped]
    skipped = [s for s in scans if s.regime_skipped]
    total_passing = sum(s.signals_emitted for s in active)
    total_rejected = sum(s.rejected_count for s in active)

    lines = [
        f"Nightly scan — {as_of}",
        f"Market regime: {regime_label}" + (f" (ADX {regime.adx:.1f})" if regime else ""),
        "",
        f"Refreshed bars: {bars_upserted:,}",
        f"Strategies run: {len(active)}",
        f"Passing signals: **{total_passing}**",
        f"Rejected (filter blocked): {total_rejected}",
    ]
    if watchlist_alerts:
        lines.append(f"Watchlist alerts fired: {watchlist_alerts}")
    lines.extend(["", "Per-strategy breakdown:"])
    for s in active:
        # Show the regime multiplier inline so the user can spot
        # heavily-dampened strategies at a glance.
        mult_str = (
            f" [regime x{s.regime_multiplier:.2f}]" if s.regime_multiplier != 1.0 else ""
        )
        lines.append(
            f"  • {s.strategy_name} v{s.strategy_version}: "
            f"{s.signals_emitted} passing / {s.rejected_count} rejected "
            f"(universe {s.universe_size}){mult_str}"
        )
    # v1 fallback — stays in case any caller still produces a row with
    # regime_skipped=True. New code path never sets this.
    for s in skipped:
        lines.append(f"  ○ {s.strategy_name} v{s.strategy_version}: paused (regime={regime_label})")
    lines.extend(_failure_block())
    body = "\n".join(lines)

    degraded = " · DEGRADED" if failures else ""
    subject = (
        f"stockscan · {total_passing} signal{'s' if total_passing != 1 else ''}"
        f" · {as_of} · {regime_label}{degraded}"
    )
    notify(subject, body, channels=channels)
