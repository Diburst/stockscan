"""The refresh pipeline (DESIGN §4.9): every fetch-and-analyze step the app
has, in dependency order, run end to end.

Two callers, one pipeline: ``stockscan jobs nightly-scan`` runs it on the
schedule and sends the summary notification; the Dashboard's Refresh
button runs it in the background (``stockscan.jobs.background``) and shows
the same steps as they complete. There is no other refresh path.

Steps, each individually fault-tolerant (a failure is logged, recorded in
``step_failures``, and the run continues):

  bars        bulk-EOD for the market's missing sessions, filtered to the
              tracked set, then a per-symbol catch-up for watched names
  macro       FRED series (HY OAS for the credit-stress flag; T-bill yields)
  regime      today's trend gate / vol scalar / credit-stress flag, forced
  composites  equal-weight sector composites, then watchlist composites
  scans       every registered strategy — skipped when nothing is new
  trades      paper trades marked to market, exits applied
  options     tonight's short-premium book (replacing today's earlier run),
              then settlement of expired proposals
  feeds       news, macro calendar, earnings, insider — each only when the
              data plan includes it and its daily cooldown has passed
  alerts      watchlist price-target alerts

The pipeline is idempotent: bars are fetched only for sessions the store
lacks, the regime row for the day is replaced, scans are skipped when no
bar arrived and a run already covers the latest bar, today's options run
is replaced, and the feeds are cooldown-gated. Clicking Refresh twice in
a row costs nothing the second time.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date

from stockscan.config import settings
from stockscan.data.backfill import (
    BulkRefreshResult,
    CatchUpResult,
    catch_up_lagging_symbols,
    missing_bulk_dates,
    refresh_recent_days_bulk,
)
from stockscan.data.macro_refresh import DEFAULT_MACRO_SERIES, refresh_macro
from stockscan.data.providers import EODHDProvider, StubProvider
from stockscan.data.providers.base import DataProvider
from stockscan.data.providers.fred import FredProvider
from stockscan.data.store import latest_daily_bar_date
from stockscan.data.tracked import tracked_symbols
from stockscan.db import session_scope
from stockscan.earnings import refresh_earnings
from stockscan.econ_events import refresh_economic_events
from stockscan.insider import refresh_insider_for_watchlist
from stockscan.news import refresh_news
from stockscan.notify import notify
from stockscan.positions.paper_store import check_auto_close, mark_to_market
from stockscan.proposals import generate_book
from stockscan.proposals.settle import settle_expired
from stockscan.proposals.store import save_run
from stockscan.refresh_log import mark_refreshed, refresh_due
from stockscan.regime import detect_regime
from stockscan.regime.store import MarketRegime
from stockscan.scan import ScanRunner, ScanSummary
from stockscan.scan.store import has_run_covering
from stockscan.strategies import STRATEGY_REGISTRY, discover_strategies
from stockscan.watchlist import check_and_fire_alerts, watchlist_symbols
from stockscan.watchlist.composite import backfill_all_lists

log = logging.getLogger(__name__)

STEPS: tuple[str, ...] = (
    "bars", "macro", "regime", "composites", "scans", "trades", "options", "feeds", "alerts",
)

# Calendar days the bulk pass may look back when the store has fallen behind.
BULK_DAYS_BACK = 7
# The news, macro-calendar and earnings feeds barely move intraday; once a
# day is enough and repeat runs inside the window make no call.
FEED_COOLDOWN_HOURS = 20

Progress = Callable[[str, int, int], None]


@dataclass(frozen=True, slots=True)
class PipelineResult:
    as_of: date
    bars_upserted: int
    caught_up: tuple[str, ...]
    scans: list[ScanSummary]
    scans_skipped: bool
    regime: MarketRegime | None
    trades_marked: int
    trades_auto_closed: int
    options_run_id: int | None
    options_settled: int
    # feed name -> one-line outcome ("12 articles", "cooldown", "not on plan")
    feeds: dict[str, str]
    watchlist_alerts_fired: int
    step_failures: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0

    @property
    def degraded(self) -> bool:
        return bool(self.step_failures)


def _provider() -> DataProvider:
    key = settings.eodhd_api_key.get_secret_value()
    return EODHDProvider(api_key=key) if key else StubProvider()


def run_pipeline(
    as_of: date | None = None,
    *,
    notify_channels=None,
    send_summary: bool = True,
    progress: Progress | None = None,
) -> PipelineResult:
    """Run every step. ``progress(label, index, total)`` is called as each
    step starts; ``send_summary`` sends the nightly notification at the end
    (the Dashboard button passes ``False`` and shows the result instead)."""
    as_of = as_of or date.today()
    job_started = time.perf_counter()
    discover_strategies()
    failures: list[str] = []
    total = len(STEPS)

    def _start(label: str) -> float:
        if progress is not None:
            progress(label, STEPS.index(label) + 1, total)
        return time.perf_counter()

    def _done(label: str, started: float) -> None:
        log.info("pipeline: step '%s' done in %.1fs", label, time.perf_counter() - started)

    # ---- bars ------------------------------------------------------------
    t = _start("bars")
    bulk = _refresh_recent_bars()
    bars_upserted = bulk.upserted
    if bulk.failed_days:
        failures.append(
            "bars refresh: %d day(s) failed (%s)"
            % (len(bulk.failed_days), ", ".join(str(d) for d in bulk.failed_days))
        )
    catch_up = _catch_up_watchlist()
    bars_upserted += catch_up.upserted
    if catch_up.failed:
        failures.append("bars catch-up failed for %s" % ", ".join(catch_up.failed))
    _done("bars", t)

    # ---- macro -----------------------------------------------------------
    t = _start("macro")
    fred_key = settings.fred_api_key.get_secret_value()
    if fred_key:
        try:
            with FredProvider(api_key=fred_key) as fred:
                macro = refresh_macro(
                    fred, DEFAULT_MACRO_SERIES, as_of.replace(year=as_of.year - 2), as_of
                )
            missing = [code for code, n in macro.items() if n is None]
            if missing:
                failures.append("macro refresh: " + ", ".join(missing))
        except Exception as exc:
            log.error("pipeline: macro refresh failed: %s", exc)
            failures.append(f"macro refresh: {exc}")
    else:
        log.warning("pipeline: FRED_API_KEY not set — credit-stress flag runs on stale HY OAS")
    _done("macro", t)

    # ---- regime (forced, so a row cached earlier today is replaced) ------
    t = _start("regime")
    regime: MarketRegime | None = None
    try:
        regime = detect_regime(as_of, force_recompute=True)
    except Exception as exc:
        log.warning("pipeline: regime detection failed: %s", exc)
        failures.append(f"regime detection: {exc}")
    _done("regime", t)

    # ---- composites (local, no API calls) ---------------------------------
    t = _start("composites")
    try:
        from stockscan.sectors.store import DEFAULT_BASE_START, refresh_sector_composites

        composites = refresh_sector_composites(DEFAULT_BASE_START, as_of)
        log.info("pipeline: rebuilt %d sector composites", len(composites))
    except Exception as exc:
        log.error("pipeline: sector-composite rebuild failed: %s", exc)
        failures.append(f"sector composites: {exc}")
    try:
        backfill_all_lists()
    except Exception as exc:
        log.error("pipeline: watchlist-composite rebuild failed: %s", exc)
        failures.append(f"watchlist composites: {exc}")
    _done("composites", t)

    # ---- scans -----------------------------------------------------------
    t = _start("scans")
    scans: list[ScanSummary] = []
    scans_skipped = False
    latest_bar = latest_daily_bar_date()
    if bars_upserted == 0 and latest_bar is not None and _scans_cover(latest_bar):
        scans_skipped = True
        log.info("pipeline: no new bars and every strategy already ran for %s — scans skipped", latest_bar)
    else:
        runner = ScanRunner()
        for name in STRATEGY_REGISTRY.names():
            try:
                scans.append(runner.run(name, as_of))
            except Exception as exc:
                log.error("scan %s failed: %s", name, exc)
                failures.append(f"scan {name}: {exc}")
    _done("scans", t)

    # ---- trades ----------------------------------------------------------
    t = _start("trades")
    trades_marked = 0
    trades_auto_closed = 0
    try:
        with session_scope() as s:
            trades_marked = mark_to_market(session=s)
            trades_auto_closed = len(check_auto_close(session=s))
    except Exception as exc:
        log.error("pipeline: paper-trade upkeep failed: %s", exc)
        failures.append(f"paper trades: {exc}")
    _done("trades", t)

    # ---- options ---------------------------------------------------------
    t = _start("options")
    options_run_id: int | None = None
    options_settled = 0
    try:
        options_run_id = save_run(generate_book(list_id=None, as_of=as_of), list_id=None, replace=True)
        settled = settle_expired(as_of)
        options_settled = settled.settled
        log.info(
            "options: saved run %d, settled %d (%d breached)",
            options_run_id, settled.settled, settled.breached,
        )
    except Exception as exc:
        log.error("pipeline: options step failed: %s", exc)
        failures.append(f"options: {exc}")
    _done("options", t)

    # ---- feeds -----------------------------------------------------------
    t = _start("feeds")
    feeds = _refresh_feeds(failures)
    _done("feeds", t)

    # ---- alerts ----------------------------------------------------------
    t = _start("alerts")
    alerts_fired = 0
    try:
        alerts_fired = len(check_and_fire_alerts(channels=notify_channels).fired)
    except Exception as exc:
        log.error("watchlist alert check failed: %s", exc)
        failures.append(f"watchlist alerts: {exc}")
    _done("alerts", t)

    result = PipelineResult(
        as_of=as_of,
        bars_upserted=bars_upserted,
        caught_up=catch_up.symbols_fetched,
        scans=scans,
        scans_skipped=scans_skipped,
        regime=regime,
        trades_marked=trades_marked,
        trades_auto_closed=trades_auto_closed,
        options_run_id=options_run_id,
        options_settled=options_settled,
        feeds=feeds,
        watchlist_alerts_fired=alerts_fired,
        step_failures=failures,
        duration_seconds=time.perf_counter() - job_started,
    )
    if send_summary:
        _send_summary(result, channels=notify_channels)
    log.info(
        "pipeline: run complete for %s — %d bars, %d scans%s, %d failures, %.1fs total",
        as_of, bars_upserted, len(scans), " (skipped)" if scans_skipped else "",
        len(failures), result.duration_seconds,
    )
    return result


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------
def _refresh_recent_bars() -> BulkRefreshResult:
    """Bulk-fetch the sessions the store lacks, up to the latest *completed*
    session — never today's before the close has printed."""
    universe = tracked_symbols()
    if not universe:
        log.warning("pipeline: universe is empty — run `stockscan refresh universe` first")
        return BulkRefreshResult(upserted=0)
    days = missing_bulk_dates(BULK_DAYS_BACK)
    if not days:
        log.info("pipeline: bars already current")
        return BulkRefreshResult(upserted=0)
    log.info("pipeline: bulk-refreshing %d days (%s..%s)", len(days), days[0], days[-1])
    with _provider() as p:
        return refresh_recent_days_bulk(p, days, filter_to=universe)


def _catch_up_watchlist() -> CatchUpResult:
    """Per-symbol fetch for watched names behind the freshest stored bar —
    the bulk pass judges freshness by the market, not by each symbol, so
    a watched name that fell behind would otherwise stay stale forever."""
    target = latest_daily_bar_date()
    watched = watchlist_symbols()
    if target is None or not watched:
        return CatchUpResult()
    with _provider() as p:
        return catch_up_lagging_symbols(p, watched, target=target)


def _scans_cover(latest_bar: date) -> bool:
    """True when every registered strategy, at its current version, already
    has a run whose as-of date reaches the latest stored bar."""
    return all(
        has_run_covering(name, STRATEGY_REGISTRY.get(name).version, latest_bar)
        for name in STRATEGY_REGISTRY.names()
    )


def _refresh_feeds(failures: list[str]) -> dict[str, str]:
    """News, macro calendar, earnings and insider — each skipped without a
    request when the data plan excludes it or its cooldown has not passed."""
    if not settings.eodhd_api_key.get_secret_value():
        return {}
    out: dict[str, str] = {}
    with _provider() as provider, session_scope() as s:
        watched = sorted(watchlist_symbols(session=s))

        if not provider.supports("news"):
            out["news"] = "not on plan"
        elif refresh_due("news", cooldown_hours=FEED_COOLDOWN_HOURS, session=s):
            try:
                res = refresh_news(provider, watchlist_symbols=watched, session=s)
                out["news"] = f"{res.articles_upserted} articles"
                mark_refreshed("news", session=s)
            except Exception as exc:
                failures.append(f"news: {exc}")
        else:
            out["news"] = "cooldown"

        if not provider.supports("econ_events"):
            out["macro calendar"] = "not on plan"
        elif refresh_due("econ_events", cooldown_hours=FEED_COOLDOWN_HOURS, session=s):
            try:
                res = refresh_economic_events(provider, session=s)
                if res.error:
                    failures.append(f"macro calendar: {res.error}")
                else:
                    out["macro calendar"] = f"{res.upserted} events"
                    mark_refreshed("econ_events", session=s)
            except Exception as exc:
                failures.append(f"macro calendar: {exc}")
        else:
            out["macro calendar"] = "cooldown"

        if not provider.supports("calendar"):
            out["earnings"] = "not on plan"
        elif not watched:
            out["earnings"] = "no watched symbols"
        elif refresh_due("earnings", cooldown_hours=FEED_COOLDOWN_HOURS, session=s):
            try:
                res = refresh_earnings(provider, watched, session=s)
                if res.error:
                    failures.append(f"earnings: {res.error}")
                else:
                    out["earnings"] = f"{res.calendar_upserted} dates, {res.trends_upserted} trend points"
                    mark_refreshed("earnings", session=s)
            except Exception as exc:
                failures.append(f"earnings: {exc}")
        else:
            out["earnings"] = "cooldown"

        try:
            res = refresh_insider_for_watchlist(provider, watched, session=s)
            if res.skipped_reason:
                out["insider"] = "not on plan"
            elif res.skipped:
                out["insider"] = "cooldown"
            elif res.error:
                failures.append(f"insider: {res.error}")
            else:
                out["insider"] = f"{res.transactions_upserted} transactions"
        except Exception as exc:
            failures.append(f"insider: {exc}")
    return out


# ---------------------------------------------------------------------------
# Summary notification (nightly only)
# ---------------------------------------------------------------------------
def _send_summary(result: PipelineResult, *, channels=None) -> None:
    as_of = result.as_of
    regime = result.regime
    regime_label = regime.regime.replace("_", " ") if regime else "unknown"
    failures = result.step_failures

    def _failure_block() -> list[str]:
        if not failures:
            return []
        return ["", "⚠ Step failures (run degraded):"] + [f"  ✗ {f}" for f in failures]

    if not result.scans and not result.scans_skipped:
        body = (
            f"Nightly run for {as_of}\n\n"
            f"Market regime: {regime_label}\n"
            f"No strategies registered. Refreshed {result.bars_upserted} bars.\n"
            + "\n".join(_failure_block())
        )
        notify(f"stockscan · {as_of}", body, channels=channels)
        return

    total_passing = sum(s.signals_emitted for s in result.scans)
    total_rejected = sum(s.rejected_count for s in result.scans)
    lines = [
        f"Nightly scan — {as_of}",
        f"Market regime: {regime_label}" + (f" ({_regime_detail(regime)})" if regime else ""),
        "",
        f"Refreshed bars: {result.bars_upserted:,}"
        + (f" (caught up {', '.join(result.caught_up)})" if result.caught_up else ""),
        f"Strategies run: {len(result.scans)}"
        + (" (skipped — nothing new since the last run)" if result.scans_skipped else ""),
        f"Passing signals: **{total_passing}**",
        f"Rejected (filter blocked): {total_rejected}",
    ]
    if result.watchlist_alerts_fired:
        lines.append(f"Watchlist alerts fired: {result.watchlist_alerts_fired}")
    if result.scans:
        lines.extend(["", "Per-strategy breakdown:"])
        for s in result.scans:
            scalar = f" [vol scalar x{s.vol_scalar:.2f}]" if s.vol_scalar != 1.0 else ""
            lines.append(
                f"  • {s.strategy_name} v{s.strategy_version}: "
                f"{s.signals_emitted} passing / {s.rejected_count} rejected "
                f"(universe {s.universe_size}){scalar}"
            )
    lines.extend(_failure_block())
    body = "\n".join(lines)

    degraded = " · DEGRADED" if failures else ""
    subject = (
        f"stockscan · {total_passing} signal{'s' if total_passing != 1 else ''}"
        f" · {as_of} · {regime_label}{degraded}"
    )
    notify(subject, body, channels=channels)


def _regime_detail(regime: MarketRegime) -> str:
    gate = "gate open" if regime.trend_gate_open else "gate closed"
    return f"{gate} {regime.days_on_side}d · vol scalar {regime.vol_multiplier:.2f}"
