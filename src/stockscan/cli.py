"""`stockscan` command-line interface.

Top-level commands:
  stockscan health                       — DB + provider + strategies connectivity
  stockscan version                      — print app version

Database:
  stockscan db migrate                   — apply pending SQL migrations
  stockscan db status                    — show applied + pending migrations
  stockscan db verify                    — detect checksum drift on applied migrations

Data refresh (provider → local store):
  stockscan refresh universe             — pull S&P 500 membership from EODHD
  stockscan refresh bars SYMBOL [...]    — backfill bars for one or more tickers
                                            (e.g. for a new watchlist name); omit
                                            args to backfill the full S&P 500 universe.
                                            Incremental on re-run.
  stockscan refresh daily --days N       — bulk-EOD recent-N-day catch-up
  stockscan refresh macro [SERIES...]    — backfill FRED macro series (HY OAS, etc.)
  stockscan refresh fundamentals         — pull EODHD fundamentals (38 cols + raw JSONB)
  stockscan refresh news                 — pull EODHD news for general feed + watchlist

Strategies:
  stockscan strategies list              — registered strategy names
  stockscan strategies show NAME         — strategy metadata + Pydantic params schema

Scanning + signals (live signal generation, persistence, version-aware admin):
  stockscan scan run STRATEGY [--all]    — run a single strategy or every registered one
  stockscan signals backfill STRATEGY    — replay scans across a date range. Skip-query
                                            is version-aware: dates last scanned under an
                                            older strategy_version get re-scanned. Default
                                            range: last 365 days, weekdays only, resumable.
  stockscan signals delete -s NAME -v V  — explicitly delete signals + strategy_runs for
                                            (strategy, version). Optional --start/--end
                                            date range. Confirms unless --yes.

Meta-labeling (XGBoost classifier scoring P(profit-take) per signal):
  stockscan ml train STRATEGY            — fit + pickle the model under ./models/. Filters
                                            training data to the CURRENT strategy_version
                                            by default; pass --strategy-version X.Y.Z to
                                            override (re-train on historical-version data).
  stockscan ml status                    — list trained models with holdout AUC

Version semantics:
  After a strategy version bump, web tools (Dashboard, /signals) AND ml train all default
  to the new version. Older-version signals are preserved in the database but inert on
  the live UI. Use ``stockscan signals delete`` with explicit --version to clean up
  prior-version data when desired.

Watchlist + technical:
  stockscan watchlist list               — current watch entries
  stockscan watchlist add SYMBOL         — add with optional --target / --direction
  stockscan watchlist remove ID
  stockscan watchlist check-alerts       — fire any pending price-target alerts now
  stockscan technical list               — registered technical indicators
  stockscan technical backfill           — fill missing scores for past signals
  stockscan technical recompute --since  — overwrite existing scores from a date

Backtesting:
  stockscan backtest run STRATEGY        — event-driven backtest, persists results
  stockscan backtest list                — saved backtest runs

Scheduled jobs (production: invoked by launchd):
  stockscan jobs nightly-scan            — refresh + scan + alerts + notify
"""

from __future__ import annotations

import json
import logging
from datetime import date

import typer
from rich.console import Console
from rich.table import Table

from stockscan import __version__
from stockscan.config import settings
from stockscan.data.backfill import backfill_universe
from stockscan.data.providers import EODHDProvider, StubProvider
from stockscan.data.providers.base import DataProvider
from stockscan.db import healthcheck
from stockscan.strategies import STRATEGY_REGISTRY, discover_strategies
from stockscan.universe import (
    all_known_symbols,
    current_constituents,
    refresh_universe,
)

console = Console()
log = logging.getLogger(__name__)

app = typer.Typer(
    help="stockscan — personal swing-trading scanner.",
    no_args_is_help=True,
    add_completion=False,
)
db_app = typer.Typer(help="Database operations.", no_args_is_help=True)
refresh_app = typer.Typer(help="Refresh data from the provider.", no_args_is_help=True)
strat_app = typer.Typer(help="Inspect registered strategies.", no_args_is_help=True)
backtest_app = typer.Typer(help="Run and inspect backtests.", no_args_is_help=True)
scan_app = typer.Typer(help="Run live or backdated scans.", no_args_is_help=True)
jobs_app = typer.Typer(help="Scheduled job orchestration.", no_args_is_help=True)
watchlist_app = typer.Typer(help="Manage the watchlist.", no_args_is_help=True)
technical_app = typer.Typer(help="Technical-confirmation scoring.", no_args_is_help=True)
ml_app = typer.Typer(help="Meta-labeling: train + inspect XGBoost models.", no_args_is_help=True)
signals_app = typer.Typer(
    help="Signal-table operations (e.g., backfill historical scans).",
    no_args_is_help=True,
)
analysis_app = typer.Typer(
    help="Per-symbol technical analysis: levels, trend, vol, options context.",
    no_args_is_help=True,
)
app.add_typer(db_app, name="db")
app.add_typer(refresh_app, name="refresh")
app.add_typer(strat_app, name="strategies")
app.add_typer(backtest_app, name="backtest")
app.add_typer(scan_app, name="scan")
app.add_typer(jobs_app, name="jobs")
app.add_typer(watchlist_app, name="watchlist")
app.add_typer(technical_app, name="technical")
app.add_typer(ml_app, name="ml")
app.add_typer(signals_app, name="signals")
app.add_typer(analysis_app, name="analysis")


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def _provider() -> DataProvider:
    """Return the configured provider; falls back to StubProvider in dev/test."""
    key = settings.eodhd_api_key.get_secret_value()
    if not key:
        if settings.is_prod:
            raise typer.BadParameter(
                "EODHD_API_KEY is not set; refusing to run with stub data in prod."
            )
        console.print("[yellow]EODHD_API_KEY not set — using StubProvider[/yellow]")
        return StubProvider()
    return EODHDProvider(api_key=key)


# ----------------------------------------------------------------------
# Top-level commands
# ----------------------------------------------------------------------
@app.command()
def version() -> None:
    """Print the app version."""
    console.print(f"stockscan {__version__}")


@app.command()
def health() -> None:
    """Run liveness checks: DB connectivity, provider reachability, etc."""
    discover_strategies()
    db = healthcheck()
    table = Table(title="stockscan health")
    table.add_column("Component")
    table.add_column("Status")
    table.add_column("Detail")
    table.add_row(
        "Database",
        "[green]ok[/green]" if db.get("ok") else "[red]fail[/red]",
        str(db.get("postgres") or db.get("error", "")),
    )
    table.add_row(
        "TimescaleDB",
        "[green]ok[/green]"
        if db.get("timescaledb") not in (None, "(missing)")
        else "[red]missing[/red]",
        str(db.get("timescaledb", "")),
    )
    table.add_row(
        "Strategies",
        f"[green]{len(STRATEGY_REGISTRY)}[/green]",
        ", ".join(STRATEGY_REGISTRY.names()) or "(none registered)",
    )
    table.add_row(
        "EODHD key",
        "[green]set[/green]"
        if settings.eodhd_api_key.get_secret_value()
        else "[yellow]not set[/yellow]",
        "",
    )
    console.print(table)


# ----------------------------------------------------------------------
# DB commands
# ----------------------------------------------------------------------
@db_app.command("migrate")
def db_migrate() -> None:
    """Apply pending SQL migrations from migrations/."""
    from stockscan.db_migrate import apply_pending, pending_migrations

    pending = pending_migrations()
    if not pending:
        console.print("[green]✓[/green] schema is up to date — no pending migrations")
        return
    for m in pending:
        console.print(f"[cyan]→[/cyan] applying {m.version}_{m.name}")
    applied = apply_pending()
    console.print(f"[green]✓[/green] applied {len(applied)} migration(s)")


@db_app.command("status")
def db_status() -> None:
    """Show applied + pending migrations."""
    from stockscan.db_migrate import (
        applied_versions,
        current_version,
        pending_migrations,
    )

    applied = applied_versions()
    pending = pending_migrations()
    cur = current_version()
    console.print(f"[bold]Current schema version:[/bold] {cur or '(none — fresh DB)'}")
    if applied:
        table = Table(title="Applied migrations")
        table.add_column("Version")
        table.add_column("Name")
        table.add_column("Applied at")
        for v, info in sorted(applied.items()):
            table.add_row(v, info["name"], str(info.get("applied_at", "")))
        console.print(table)
    if pending:
        table = Table(title="Pending migrations")
        table.add_column("Version")
        table.add_column("Name")
        for m in pending:
            table.add_row(m.version, m.name)
        console.print(table)
    else:
        console.print("[green]✓[/green] no pending migrations")


@db_app.command("verify")
def db_verify() -> None:
    """Detect checksum drift between migrations on disk and what's recorded."""
    from stockscan.db_migrate import verify_checksums

    drift = verify_checksums()
    if not drift:
        console.print("[green]✓[/green] all applied migrations match their on-disk checksums")
        return
    console.print("[red]✗ checksum drift detected:[/red]")
    for ver, applied_cs, current_cs in drift:
        console.print(f"  {ver}: applied={applied_cs[:12]}…, current={current_cs[:12]}…")
    raise typer.Exit(1)


# ----------------------------------------------------------------------
# Refresh commands
# ----------------------------------------------------------------------
@refresh_app.command("universe")
def refresh_universe_cmd() -> None:
    """Pull historical + current S&P 500 membership from the provider."""
    with _provider_ctx() as p:
        n = refresh_universe(p)
    console.print(f"[green]✓[/green] universe refreshed: {n} membership rows upserted")


@refresh_app.command("fundamentals")
def refresh_fundamentals_cmd(
    symbols: list[str] = typer.Argument(None, help="Symbols to refresh; default = current S&P 500"),
    current_only: bool = typer.Option(
        False,
        "--current-only",
        help="Restrict to current S&P 500 (skips ex-members)",
    ),
) -> None:
    """Pull EODHD fundamentals (market cap, sector, ratios, ...) for each symbol.

    Heavy: one API call per symbol (~500 for full universe), each call returning
    hundreds of KB. Plan to run weekly, not daily — most fields change quarterly.
    """
    from stockscan.fundamentals import refresh_fundamentals

    if not symbols:
        symbols = current_constituents() if current_only else all_known_symbols()
        if not symbols:
            console.print(
                "[yellow]Universe is empty. Run `stockscan refresh universe` first.[/yellow]"
            )
            raise typer.Exit(1)
        scope = "current S&P 500" if current_only else "all ever-members of S&P 500"
        console.print(
            f"[cyan]→[/cyan] refreshing fundamentals for {len(symbols)} symbols ({scope})"
        )
    else:
        console.print(f"[cyan]→[/cyan] refreshing fundamentals for {len(symbols)} symbols")

    with _provider_ctx() as p:
        results = refresh_fundamentals(p, symbols)
    ok = sum(1 for v in results.values() if v == "ok")
    missing = sum(1 for v in results.values() if v == "missing")
    failed = sum(1 for v in results.values() if v == "error")
    console.print(f"[green]✓[/green] {ok:,} fetched · {missing:,} missing · {failed:,} failed")


@refresh_app.command("daily")
def refresh_daily_cmd(
    days: int = typer.Option(
        5,
        "--days",
        help="Number of recent trading days to refresh via the bulk endpoint",
    ),
    current_only: bool = typer.Option(
        False,
        "--current-only",
        help="Filter the upsert to current S&P 500 only (skips ex-members)",
    ),
) -> None:
    """Bulk refresh the last N trading days — ONE API call per day, all symbols.

    The right command for daily updates after the initial backfill is done.
    Massively cheaper than `refresh bars` for small windows: 1500x fewer
    API calls when refreshing a single day.
    """
    from datetime import date as _date

    from stockscan.data.backfill import refresh_recent_days_bulk, trading_days_since

    target_days = trading_days_since(_date.today() - typedelta_days(days), _date.today())
    if not target_days:
        console.print("[yellow]No trading days in window.[/yellow]")
        return

    filter_to = None
    if current_only:
        filter_to = set(current_constituents())
    else:
        filter_to = set(all_known_symbols())
    if not filter_to:
        console.print("[yellow]Universe is empty. Run `stockscan refresh universe` first.[/yellow]")
        raise typer.Exit(1)

    console.print(
        f"[cyan]→[/cyan] bulk-refreshing {len(target_days)} trading days "
        f"({target_days[0]} → {target_days[-1]}), filter to {len(filter_to)} symbols"
    )
    with _provider_ctx() as p:
        n = refresh_recent_days_bulk(p, target_days, filter_to=filter_to)
    console.print(f"[green]✓[/green] {n:,} bars upserted")


def typedelta_days(n: int):
    """tiny helper so the CLI doesn't need to import timedelta."""
    from datetime import timedelta

    return timedelta(days=n)


@refresh_app.command("bars")
def refresh_bars_cmd(
    symbols: list[str] = typer.Argument(
        None,
        help=(
            "One or more symbols to backfill (e.g. 'AAPL' or 'AAPL MSFT NVDA'). "
            "Omit to backfill the entire historical S&P 500 universe."
        ),
    ),
    start: str = typer.Option("2007-01-01", "--start", help="ISO date for initial backfill"),
    end: str | None = typer.Option(None, "--end", help="ISO date; default = today"),
    current_only: bool = typer.Option(
        False,
        "--current-only",
        help="Only fetch current S&P 500 (skips ~700 historical-only members; faster but reintroduces survivorship bias for old backtests)",
    ),
    exchange: str = typer.Option(
        "US",
        "--exchange",
        help="EODHD exchange suffix (e.g., 'US' for equities, 'INDX' for cash indices like VIX)",
    ),
) -> None:
    """Backfill / incrementally update daily OHLCV bars from the provider.

    Two modes:

    * **Per-symbol** — pass one or more tickers as positional args. This is
      the right command when you want bars for a name you're watching or
      analyzing but haven't necessarily wired into a scanner. No strategy
      runs, no signals are generated — just OHLCV into the local ``bars``
      table, which then feeds the watchlist, technical analysis, and
      backtest tooling.
          stockscan refresh bars AAPL
          stockscan refresh bars AAPL MSFT NVDA --start 2015-01-01

    * **Universe-wide** — omit the positional args to backfill every symbol
      ever in the S&P 500 (current + historical members). Restoring
      historical members eliminates survivorship bias on backtests.
          stockscan refresh bars                        # all ever-members
          stockscan refresh bars --current-only         # current ~500 only

    All invocations are **incremental** on re-run: per symbol, only the
    window from ``last_cached_date - 5 days`` to ``end`` is re-fetched,
    so a daily refresh after the initial backfill takes seconds.

    Use ``--exchange INDX`` for cash indices like VIX:
        stockscan refresh bars VIX --exchange INDX
    """
    start_d = date.fromisoformat(start)
    end_d = date.fromisoformat(end) if end else date.today()
    if not symbols:
        symbols = current_constituents() if current_only else all_known_symbols()
        if not symbols:
            console.print(
                "[yellow]Universe is empty. Run `stockscan refresh universe` first.[/yellow]"
            )
            raise typer.Exit(1)
        scope = "current S&P 500" if current_only else "all ever-members of S&P 500"
        console.print(
            f"[cyan]→[/cyan] backfilling {len(symbols)} symbols ({scope}) "
            f"from {start_d} to {end_d} (exchange={exchange})"
        )
    else:
        # Normalise + de-dup user-supplied tickers; provider expects upper-case.
        symbols = sorted({s.strip().upper() for s in symbols if s and s.strip()})
        if not symbols:
            console.print("[yellow]No valid symbols provided.[/yellow]")
            raise typer.Exit(1)
        console.print(
            f"[cyan]→[/cyan] backfilling {len(symbols)} symbol(s) "
            f"({', '.join(symbols)}) from {start_d} to {end_d} "
            f"(exchange={exchange}, incremental on re-run)"
        )
    with _provider_ctx() as p:
        results = backfill_universe(p, symbols, start=start_d, end=end_d, exchange=exchange)
    upserts = sum(v for v in results.values() if v >= 0)
    failed = sum(1 for v in results.values() if v < 0)
    console.print(
        f"[green]✓[/green] {upserts:,} bars upserted across {len(results)} symbols "
        f"({failed} failed)"
    )


@refresh_app.command("macro")
def refresh_macro_cmd(
    series: list[str] = typer.Argument(
        None,
        help="FRED series codes to refresh; default = ['BAMLH0A0HYM2'] (HY OAS)",
    ),
    start: str = typer.Option(
        "2007-01-01",
        "--start",
        help="ISO date for initial backfill",
    ),
    end: str | None = typer.Option(
        None,
        "--end",
        help="ISO date; default = today",
    ),
) -> None:
    """Pull macro time series from FRED into the ``macro_series`` table.

    Default series is ``BAMLH0A0HYM2`` (ICE BofA US High Yield OAS) — the
    credit component of the v2 regime composite. The detector needs at
    least 252 trailing observations (~1 year) to compute the percentile
    rank, so the default 2007-01-01 start gives ample warmup history.

    Examples:
        stockscan refresh macro                       # HY OAS, 2007-today
        stockscan refresh macro BAMLH0A0HYM2 BAMLC0A0CMEY  # HY + IG OAS
        stockscan refresh macro --start 2020-01-01    # shorter window
    """
    from stockscan.data.macro_store import upsert_macro_series
    from stockscan.data.providers.fred import FredError, FredProvider

    if not series:
        series = ["BAMLH0A0HYM2"]

    start_d = date.fromisoformat(start)
    end_d = date.fromisoformat(end) if end else date.today()

    fred_key = settings.fred_api_key.get_secret_value()
    if not fred_key:
        console.print(
            "[red]✗[/red] FRED_API_KEY is not set. Get a free key from "
            "https://fred.stlouisfed.org/docs/api/api_key.html and add "
            "FRED_API_KEY=... to your .env."
        )
        raise typer.Exit(1)

    console.print(
        f"[cyan]→[/cyan] refreshing {len(series)} series from FRED ({start_d} to {end_d})"
    )

    total = 0
    failed: list[str] = []
    with FredProvider(api_key=fred_key) as p:
        for code in series:
            try:
                rows = p.get_macro_series(code, start_d, end_d)
            except FredError as exc:
                console.print(f"  [red]✗[/red] {code}: {exc}")
                failed.append(code)
                continue
            n = upsert_macro_series(rows)
            console.print(f"  [green]✓[/green] {code}: {n:,} observations")
            total += n

    if failed:
        console.print(f"[yellow]{len(failed)} series failed: {', '.join(failed)}[/yellow]")
    console.print(f"[green]✓[/green] {total:,} total observations upserted")


@refresh_app.command("news")
def refresh_news_cmd(
    days_back: int = typer.Option(
        7,
        "--days-back",
        help="How many days of history to pull on each call",
    ),
) -> None:
    """Pull recent financial news from EODHD into the local store.

    Pulls the configured general-market feed (symbols + tags from
    ``news_feed_config``, auto-seeded with sensible defaults if you've
    never edited it) plus any watchlist symbols not already covered.
    Idempotent — re-running on the same day re-upserts articles with a
    refreshed ``fetched_at`` timestamp.

    Suggested cadence: daily after the nightly scan. Add to launchd or
    fold into the existing nightly job.
    """
    from stockscan.news import refresh_news
    from stockscan.watchlist import watchlist_symbols

    fred_unused = (
        settings  # silence "imported but unused" if needed; settings is already used elsewhere
    )
    del fred_unused

    api_key = settings.eodhd_api_key.get_secret_value()
    if not api_key:
        console.print("[red]✗[/red] EODHD_API_KEY is not set. Add it to your .env to fetch news.")
        raise typer.Exit(1)

    console.print(f"[cyan]→[/cyan] refreshing news (last {days_back} days)")
    with EODHDProvider(api_key=api_key) as p:
        result = refresh_news(p, days_back=days_back, watchlist_symbols=watchlist_symbols())

    console.print(
        f"[green]✓[/green] {result.articles_upserted:,} articles upserted "
        f"({result.api_calls} API calls, {result.failures} failures, "
        f"took {(result.finished_at - result.started_at).total_seconds():.1f}s)"
    )
    if result.last_fetched_at:
        console.print(f"  last fetch timestamp: {result.last_fetched_at:%Y-%m-%d %H:%M UTC}")


# ----------------------------------------------------------------------
# Strategies commands
# ----------------------------------------------------------------------
@strat_app.command("list")
def strategies_list() -> None:
    """List registered strategies."""
    discover_strategies()
    if not STRATEGY_REGISTRY:
        console.print(
            "[yellow]No strategies registered.[/yellow] "
            "(Drop a file in src/stockscan/strategies/ that subclasses Strategy.)"
        )
        return
    table = Table(title="Registered strategies")
    table.add_column("Name")
    table.add_column("Version")
    table.add_column("Display name")
    table.add_column("Tags")
    for cls in STRATEGY_REGISTRY.all():
        table.add_row(
            cls.name,
            cls.version,
            cls.display_name,
            ", ".join(cls.tags),
        )
    console.print(table)


@strat_app.command("show")
def strategies_show(name: str) -> None:
    """Show metadata + JSON schema for a strategy's params."""
    discover_strategies()
    cls = STRATEGY_REGISTRY.get(name)
    console.print(f"[bold]{cls.display_name}[/bold]  ({cls.name} v{cls.version})")
    if cls.description:
        console.print(f"\n{cls.description}\n")
    if cls.tags:
        console.print(f"Tags: {', '.join(cls.tags)}")
    console.print(f"Default risk per trade: {cls.default_risk_pct:.2%}")
    console.print(f"Required history: {cls.required_history.__doc__ or 'see source'}")
    console.print("\n[bold]Params JSON Schema:[/bold]")
    console.print_json(json.dumps(cls.params_json_schema()))


# ----------------------------------------------------------------------
# Technical-score commands
# ----------------------------------------------------------------------
@technical_app.command("list")
def technical_list_cmd() -> None:
    """Show registered technical indicators."""
    from stockscan.technical import TECH_REGISTRY, discover_technical_indicators

    discover_technical_indicators()
    if not TECH_REGISTRY:
        console.print("[yellow]No technical indicators registered.[/yellow]")
        return
    table = Table(title="Registered technical indicators")
    table.add_column("Name")
    table.add_column("Description")
    table.add_column("Default params")
    for cls in TECH_REGISTRY.all():
        defaults = cls.params_model().model_dump()
        table.add_row(
            cls.name,
            cls.description,
            ", ".join(f"{k}={v}" for k, v in defaults.items()),
        )
    console.print(table)


def _backfill_tech_scores(
    *,
    since: str | None,
    limit: int,
    strategy: str | None,
    force: bool,
) -> tuple[int, int, int]:
    """Shared work for backfill / recompute. Returns (succeeded, skipped, failed)."""
    from datetime import date as _date
    from datetime import timedelta as _td

    from sqlalchemy import text

    from stockscan.data.store import get_bars
    from stockscan.db import session_scope
    from stockscan.technical import compute_technical_score, upsert_score

    discover_strategies()

    where_clauses = []
    params: dict[str, object] = {}
    if not force:
        where_clauses.append(
            "NOT EXISTS (SELECT 1 FROM technical_scores t "
            "WHERE t.symbol = s.symbol AND t.as_of_date = s.as_of_date "
            "AND t.strategy_name = s.strategy_name)"
        )
    if since:
        where_clauses.append("s.as_of_date >= :since")
        params["since"] = _date.fromisoformat(since)
    if strategy:
        where_clauses.append("s.strategy_name = :strat")
        params["strat"] = strategy
    where_sql = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    limit_sql = f" LIMIT {int(limit)}" if limit > 0 else ""

    sql = text(
        f"""
        SELECT s.signal_id, s.symbol, s.strategy_name, s.as_of_date
        FROM signals s
        {where_sql}
        ORDER BY s.as_of_date DESC, s.symbol
        {limit_sql}
        """
    )

    succeeded = skipped = failed = 0
    with session_scope() as sess:
        rows = sess.execute(sql, params).all()
    if not rows:
        return 0, 0, 0

    console.print(f"[cyan]→[/cyan] processing {len(rows):,} signal(s)")

    # Group rows by symbol so we load bars once per symbol per range.
    bars_cache: dict[str, pd.DataFrame] = {}  # type: ignore[name-defined]

    for i, row in enumerate(rows, 1):
        if i % 200 == 0:
            console.print(f"  … {i:,} processed")
        try:
            strategy_cls = STRATEGY_REGISTRY.get(row.strategy_name)
        except KeyError:
            skipped += 1
            log.debug(
                "tech-backfill: skipping signal %s — strategy %s not registered",
                row.signal_id,
                row.strategy_name,
            )
            continue
        try:
            bars = bars_cache.get(row.symbol)
            if bars is None or bars.index[-1].date() < row.as_of_date:
                # Load a year of history up to the most recent date we'll need
                # for this symbol. Re-querying for later dates is rare since
                # we sort DESC by date.
                end = row.as_of_date
                start = end - _td(days=400)
                bars = get_bars(row.symbol, start, end)
                bars_cache[row.symbol] = bars
            view = bars[bars.index.date <= row.as_of_date] if not bars.empty else bars
            if view.empty:
                skipped += 1
                continue
            view.attrs["symbol"] = row.symbol
            score = compute_technical_score(strategy_cls, view, row.as_of_date)
            if score is None:
                skipped += 1
                continue
            upsert_score(row.symbol, row.as_of_date, row.strategy_name, score)
            succeeded += 1
        except Exception as exc:
            failed += 1
            log.error("tech-backfill failed for signal %s: %s", row.signal_id, exc)

    return succeeded, skipped, failed


@technical_app.command("backfill")
def technical_backfill_cmd(
    since: str | None = typer.Option(
        None, "--since", help="Only process signals on or after this ISO date"
    ),
    limit: int = typer.Option(0, "--limit", help="Max signals to process (0 = all)"),
    strategy: str | None = typer.Option(
        None, "--strategy", help="Restrict to one strategy (e.g. rsi2_meanrev)"
    ),
) -> None:
    """Compute technical scores for existing signals that don't have one.

    Idempotent: signals that already have a score are skipped (use
    `technical recompute` to overwrite). Uses local bars only — no API calls.
    """
    succeeded, skipped, failed = _backfill_tech_scores(
        since=since, limit=limit, strategy=strategy, force=False
    )
    if succeeded == skipped == failed == 0:
        console.print("[green]✓[/green] no missing tech scores")
        return
    console.print(
        f"[green]✓[/green] {succeeded:,} scored · {skipped:,} skipped · {failed:,} failed"
    )


@technical_app.command("recompute")
def technical_recompute_cmd(
    since: str | None = typer.Option(
        None, "--since", help="Only process signals on or after this ISO date"
    ),
    limit: int = typer.Option(0, "--limit", help="Max signals to process (0 = all)"),
    strategy: str | None = typer.Option(None, "--strategy"),
    yes: bool = typer.Option(False, "--yes", help="Skip confirmation"),
) -> None:
    """Recompute and OVERWRITE technical scores. Use after changing scoring
    formulas or indicator parameters."""
    if not yes:
        ok = typer.confirm(
            "This will overwrite existing technical scores. Continue?", default=False
        )
        if not ok:
            console.print("[yellow]Aborted.[/yellow]")
            raise typer.Exit(0)
    succeeded, skipped, failed = _backfill_tech_scores(
        since=since, limit=limit, strategy=strategy, force=True
    )
    console.print(
        f"[green]✓[/green] {succeeded:,} recomputed · {skipped:,} skipped · {failed:,} failed"
    )


# ----------------------------------------------------------------------
# Watchlist commands
# ----------------------------------------------------------------------
@watchlist_app.command("list")
def watchlist_list_cmd() -> None:
    """Show the watchlist with latest price and target status."""
    from stockscan.watchlist import list_watchlist

    items = list_watchlist()
    if not items:
        console.print("[yellow]Watchlist is empty.[/yellow]")
        return
    table = Table(title="Watchlist")
    table.add_column("Symbol")
    table.add_column("Last close", justify="right")
    table.add_column("Δ", justify="right")
    table.add_column("Volume", justify="right")
    table.add_column("Target")
    table.add_column("Alert")
    for w in items:
        last = f"${float(w.last_close):.2f}" if w.last_close is not None else "—"
        pct = f"{w.pct_change_today * 100:+.2f}%" if w.pct_change_today is not None else "—"
        vol = f"{w.last_volume:,}" if w.last_volume else "—"
        target = (
            f"{w.target_direction} ${float(w.target_price):.2f}"
            if w.target_price is not None
            else "—"
        )
        alert = (
            ("[green]armed[/green]" if w.alert_enabled else "[yellow]off[/yellow]")
            if w.target_price is not None
            else "—"
        )
        if w.target_satisfied and w.alert_enabled:
            alert = "[bold red]TRIGGERED[/bold red]"
        table.add_row(w.symbol, last, pct, vol, target, alert)
    console.print(table)


@watchlist_app.command("add")
def watchlist_add_cmd(
    symbol: str,
    target: float | None = typer.Option(None, "--target", help="Price target"),
    direction: str | None = typer.Option(None, "--direction", help="'above' or 'below'"),
    note: str | None = typer.Option(None, "--note"),
) -> None:
    """Add a symbol to the watchlist."""
    from decimal import Decimal as _Dec

    from stockscan.watchlist import add_to_watchlist

    if (target is None) != (direction is None):
        console.print("[red]--target and --direction must be set together.[/red]")
        raise typer.Exit(1)
    wid = add_to_watchlist(
        symbol,
        target_price=_Dec(str(target)) if target is not None else None,
        target_direction=direction,  # type: ignore[arg-type]
        note=note,
    )
    console.print(f"[green]✓[/green] added watchlist item #{wid} ({symbol.upper()})")


@watchlist_app.command("remove")
def watchlist_remove_cmd(watchlist_id: int) -> None:
    """Remove a watchlist item by ID."""
    from stockscan.watchlist import remove_from_watchlist

    remove_from_watchlist(watchlist_id)
    console.print(f"[green]✓[/green] removed watchlist item #{watchlist_id}")


@watchlist_app.command("check-alerts")
def watchlist_check_alerts_cmd() -> None:
    """Run the alert check now (without the rest of the nightly job)."""
    from stockscan.watchlist import check_and_fire_alerts

    result = check_and_fire_alerts()
    console.print(f"[green]✓[/green] {len(result.fired)} alert(s) fired")
    for it in result.fired:
        console.print(f"  • {it.symbol} crossed {it.target_direction} ${it.target_price}")


# ----------------------------------------------------------------------
# Jobs (scheduler entry points)
# ----------------------------------------------------------------------
@jobs_app.command("nightly-scan")
def jobs_nightly_scan(
    as_of: str | None = typer.Option(None, "--as-of", help="ISO date; default = today"),
) -> None:
    """Bulk-refresh recent bars, run every strategy, send a summary."""
    from datetime import date as _date

    from stockscan.jobs import run_nightly_scan

    as_of_d = _date.fromisoformat(as_of) if as_of else _date.today()
    console.print(f"[cyan]→[/cyan] nightly scan as of {as_of_d}")
    result = run_nightly_scan(as_of_d)

    table = Table(title=f"Nightly scan — {result.as_of}")
    table.add_column("Strategy")
    table.add_column("Passing", justify="right")
    table.add_column("Rejected", justify="right")
    table.add_column("Universe", justify="right")
    for s in result.scans:
        table.add_row(
            s.strategy_name,
            f"[green]{s.signals_emitted}[/green]",
            f"[yellow]{s.rejected_count}[/yellow]",
            str(s.universe_size),
        )
    console.print(table)
    console.print(f"[green]✓[/green] {result.bars_upserted:,} bars refreshed")


# ----------------------------------------------------------------------
# Scan commands
# ----------------------------------------------------------------------
@scan_app.command("run")
def scan_run(
    strategy: str = typer.Argument(..., help="Registered strategy name"),
    as_of: str | None = typer.Option(None, "--as-of", help="ISO date; default = today"),
    all_strategies: bool = typer.Option(False, "--all", help="Run every registered strategy"),
) -> None:
    """Run a strategy (or all strategies) and persist signals to the DB."""
    from datetime import date as _date

    from stockscan.scan import ScanRunner

    discover_strategies()
    targets = STRATEGY_REGISTRY.names() if all_strategies else [strategy]
    as_of_d = _date.fromisoformat(as_of) if as_of else _date.today()

    table = Table(title=f"Scan results — as of {as_of_d}")
    table.add_column("Strategy")
    table.add_column("Universe", justify="right")
    table.add_column("Passing", justify="right")
    table.add_column("Rejected", justify="right")
    table.add_column("Run ID", justify="right")

    runner = ScanRunner()
    for name in targets:
        try:
            summary = runner.run(name, as_of_d)
            table.add_row(
                name,
                str(summary.universe_size),
                f"[green]{summary.signals_emitted}[/green]",
                f"[yellow]{summary.rejected_count}[/yellow]",
                str(summary.run_id),
            )
        except Exception as exc:
            table.add_row(name, "—", "—", "—", f"[red]error: {exc}[/red]")
    console.print(table)


# ----------------------------------------------------------------------
# Backtest commands
# ----------------------------------------------------------------------
@backtest_app.command("run")
def backtest_run(
    strategy: str = typer.Argument(..., help="Registered strategy name (e.g. rsi2_meanrev)"),
    start: str = typer.Option("2020-01-01", "--from", help="ISO start date"),
    end: str | None = typer.Option(None, "--to", help="ISO end date; default = today"),
    capital: float = typer.Option(1_000_000.0, "--capital"),
    risk_pct: float = typer.Option(0.01, "--risk-pct"),
    slippage_bps: float = typer.Option(5.0, "--slippage-bps"),
    commission: float = typer.Option(0.0, "--commission"),
    universe: list[str] | None = typer.Option(
        None, "--symbol", "-s", help="Restrict to these symbols (repeatable)"
    ),
    save: bool = typer.Option(True, "--save/--no-save", help="Persist results to DB"),
    note: str | None = typer.Option(None, "--note"),
) -> None:
    """Run a backtest and print the performance report."""
    from datetime import date as _date
    from decimal import Decimal as _Dec

    from stockscan.backtest import (
        BacktestConfig,
        BacktestEngine,
        FixedBpsSlippage,
    )

    discover_strategies()
    cls = STRATEGY_REGISTRY.get(strategy)
    cfg = BacktestConfig(
        strategy_cls=cls,
        params=cls.params_model(),
        start_date=_date.fromisoformat(start),
        end_date=_date.fromisoformat(end) if end else _date.today(),
        starting_capital=_Dec(str(capital)),
        risk_pct=_Dec(str(risk_pct)),
        commission_per_trade=_Dec(str(commission)),
        slippage=FixedBpsSlippage(bps=_Dec(str(slippage_bps))),
        universe=universe,
    )
    console.print(
        f"[cyan]Running {cls.display_name} {cls.version}[/cyan] "
        f"on {len(universe) if universe else 'historical S&P 500'} symbols, "
        f"{cfg.start_date} → {cfg.end_date}"
    )
    engine = BacktestEngine(cfg)
    result = engine.run()

    table = Table(title=f"{cls.display_name} backtest report")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    r = result.report
    table.add_row("# Trades", f"{r.num_trades}")
    table.add_row("Win rate", f"{r.win_rate:.1%}")
    table.add_row("Avg win", f"{r.avg_win_pct:.2%}")
    table.add_row("Avg loss", f"{r.avg_loss_pct:.2%}")
    table.add_row("Profit factor", f"{r.profit_factor:.2f}")
    table.add_row("Expectancy / trade", f"{r.expectancy_pct:.2%}")
    # R-multiple aggregates: only meaningful for trades that recorded a stop.
    r_values = [t.r_multiple for t in result.trades if t.r_multiple is not None]
    if r_values:
        avg_r = sum(r_values) / len(r_values)
        table.add_row("Avg R-multiple", f"{avg_r:+.2f}R")
        table.add_row("Best R-multiple", f"{max(r_values):+.2f}R")
        table.add_row("Worst R-multiple", f"{min(r_values):+.2f}R")
    table.add_row("Total return", f"{r.total_return_pct:.1%}")
    table.add_row("CAGR", f"{r.cagr:.2%}")
    table.add_row("Sharpe", f"{r.sharpe:.2f}")
    table.add_row("Sortino", f"{r.sortino:.2f}")
    table.add_row("Max drawdown", f"{r.max_drawdown_pct:.2%}")
    table.add_row("Max DD duration (days)", f"{r.max_drawdown_days}")
    table.add_row("Exposure", f"{r.exposure_pct:.1%}")
    console.print(table)

    if save:
        try:
            from stockscan.backtest.store import save_run

            run_id = save_run(result, note=note)
            console.print(f"[green]✓[/green] saved as backtest run #{run_id}")
        except Exception as exc:
            console.print(f"[yellow]⚠[/yellow] could not persist results: {exc}")


@backtest_app.command("list")
def backtest_list(
    strategy: str | None = typer.Option(None, "--strategy"),
    limit: int = typer.Option(20, "--limit"),
) -> None:
    """List recent backtest runs from the database."""
    from stockscan.backtest.store import list_runs

    runs = list_runs(strategy_name=strategy, limit=limit)
    if not runs:
        console.print("[yellow]No backtest runs found.[/yellow]")
        return
    table = Table(title="Recent backtest runs")
    for col in ("run_id", "strategy", "version", "from", "to", "trades", "ending_equity"):
        table.add_column(col)
    for r in runs:
        table.add_row(
            str(r["run_id"]),
            str(r["strategy_name"]),
            str(r["strategy_version"]),
            str(r["start_date"]),
            str(r["end_date"]),
            str(r.get("num_trades", "")),
            f"${float(r['ending_equity']):,.0f}" if r.get("ending_equity") else "",
        )
    console.print(table)


# ----------------------------------------------------------------------
# Provider context manager (handles cleanup)
# ----------------------------------------------------------------------
from collections.abc import Iterator
from contextlib import contextmanager


@contextmanager
def _provider_ctx() -> Iterator[DataProvider]:
    p = _provider()
    try:
        yield p
    finally:
        close = getattr(p, "close", None)
        if callable(close):
            close()


# ----------------------------------------------------------------------
# Meta-labeling (ml) commands
# ----------------------------------------------------------------------
@ml_app.command("train")
def ml_train_cmd(
    strategy: str = typer.Argument(..., help="Strategy name to train a model for"),
    holding_days: int = typer.Option(
        20, "--holding-days", help="Triple-barrier max holding window (trading days)"
    ),
    profit_take_atr_mult: float = typer.Option(
        2.0, "--pt-atr", help="Profit-take barrier in ATR multiples"
    ),
    holdout_fraction: float = typer.Option(
        0.2, "--holdout", help="Newest fraction reserved as holdout"
    ),
    min_rows: int = typer.Option(
        100, "--min-rows", help="Minimum labeled rows required to fit"
    ),
    model_version: str = typer.Option(
        "1.0.0",
        "--model-version",
        help="Tag stamped into the model artifact. Bump when feature schema changes.",
    ),
    strategy_version: str | None = typer.Option(
        None,
        "--strategy-version",
        help=(
            "Filter training data to this strategy version. Default = current "
            "registered version (so re-training after a strategy upgrade ignores "
            "older-version signals automatically). Pass an explicit version to "
            "re-fit a model on historical-version signals."
        ),
    ),
) -> None:
    """Train (or re-train) the XGBoost meta-labeling classifier for a strategy.

    Pulls every persisted signal for the strategy, builds features from the
    bars store, applies the triple-barrier label, and fits XGBoost. The
    pickled artifact lands in ``./models/<strategy>/`` and is picked up
    automatically by the next scan run.

    Requires the ``[ml]`` extra: ``uv sync --extra ml``.
    """
    from stockscan.ml import train_model
    from stockscan.ml.predict import clear_cache

    discover_strategies()
    if strategy not in STRATEGY_REGISTRY.names():
        console.print(
            f"[red]✗[/red] Unknown strategy {strategy!r}. "
            f"Registered: {', '.join(STRATEGY_REGISTRY.names())}"
        )
        raise typer.Exit(1)

    resolved_strategy_v = (
        strategy_version
        if strategy_version is not None
        else STRATEGY_REGISTRY.get(strategy).version
    )
    console.print(
        f"[cyan]→[/cyan] training meta-label model for "
        f"[bold]{strategy}[/bold] v{resolved_strategy_v}…"
    )
    try:
        result = train_model(
            strategy,
            model_version=model_version,
            strategy_version=strategy_version,
            holding_days=holding_days,
            profit_take_atr_mult=profit_take_atr_mult,
            holdout_fraction=holdout_fraction,
            min_rows=min_rows,
        )
    except RuntimeError as exc:
        msg = str(exc)
        console.print(f"[red]✗[/red] {msg}")
        # Specific actionable hint for the most common failure: not
        # enough labeled rows. We guide the user straight at the
        # backfill command rather than leaving them to figure it out.
        if "usable rows" in msg or "No historical signals" in msg:
            console.print(
                f"\n[cyan]→[/cyan] To populate historical signals, run:\n"
                f"    [bold]stockscan signals backfill {strategy}[/bold]\n"
                f"  (default: 1 year of daily scans, resumable, ~250 runs)\n"
            )
        raise typer.Exit(1) from exc

    # Drop any cached model in the predict layer so the next call sees the
    # freshly-saved artifact without an app restart.
    clear_cache()

    table = Table(title=f"Meta-label trained: {strategy}")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", justify="right")
    table.add_row("Signals seen", f"{result.n_signals_seen:,}")
    table.add_row("Train rows", f"{result.n_train_rows:,}")
    table.add_row("Holdout rows", f"{result.n_holdout_rows:,}")
    table.add_row("Base rate (winners)", f"{result.base_rate:.1%}")
    table.add_row(
        "Train AUC", f"[bold]{result.train_auc:.3f}[/bold]"
    )
    holdout_color = "green" if result.usable else "yellow"
    table.add_row(
        "Holdout AUC",
        f"[bold {holdout_color}]{result.holdout_auc:.3f}[/bold {holdout_color}]",
    )
    table.add_row("Artifact", result.artifact_path)
    console.print(table)

    if not result.usable:
        console.print(
            "[yellow]⚠ Holdout AUC ≤ 0.55 — the model has limited statistical "
            "power. Consider widening the backtest window before relying on "
            "the meta-label score.[/yellow]"
        )


@ml_app.command("status")
def ml_status_cmd() -> None:
    """List trained meta-label models with timestamps and metrics."""
    from stockscan.ml import list_models

    models = list_models()
    if not models:
        console.print(
            "[yellow]No trained models found.[/yellow] "
            "Run `stockscan ml train <strategy>` to fit one."
        )
        return

    table = Table(title="Meta-label models")
    table.add_column("Strategy", style="cyan")
    table.add_column("Version")
    table.add_column("Fit at", style="dim")
    table.add_column("Train rows", justify="right")
    table.add_column("Base rate", justify="right")
    table.add_column("Holdout AUC", justify="right")
    for a in models:
        holdout_auc = a.holdout_metrics.get("auc")
        auc_str = f"{holdout_auc:.3f}" if holdout_auc is not None else "—"
        # Color holdout AUC as a quick eyeball signal.
        if holdout_auc is None or holdout_auc < 0.50:
            auc_str = f"[red]{auc_str}[/red]"
        elif holdout_auc < 0.55:
            auc_str = f"[yellow]{auc_str}[/yellow]"
        else:
            auc_str = f"[green]{auc_str}[/green]"
        table.add_row(
            a.strategy_name,
            a.model_version,
            a.fit_at.strftime("%Y-%m-%d %H:%M UTC"),
            f"{a.n_train_rows:,}",
            f"{a.base_rate:.1%}",
            auc_str,
        )
    console.print(table)


# ----------------------------------------------------------------------
# Signals (backfill) commands
# ----------------------------------------------------------------------
@signals_app.command("backfill")
def signals_backfill_cmd(
    strategy: str = typer.Argument(
        ...,
        help="Strategy to backfill (or 'all' for every registered strategy)",
    ),
    start: str | None = typer.Option(
        None,
        "--start",
        help="ISO start date; default = today minus 365 days",
    ),
    end: str | None = typer.Option(
        None,
        "--end",
        help="ISO end date; default = today",
    ),
    every: int = typer.Option(
        1,
        "--every",
        min=1,
        max=30,
        help="Run a scan every N business days (1 = every weekday)",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Re-run scans for dates that already have a strategy_runs row",
    ),
) -> None:
    """Backfill historical signals for a strategy by replaying scans across a date range.

    Walks weekdays in [start, end], skipping dates that already have a
    strategy_runs row (use --force to override). For each missing date,
    runs the same scanner the live nightly job uses, persisting passing
    and rejected signals to the signals table.

    Resumable: kill the command and restart it; only the missing dates
    will be processed.

    Default range is one calendar year ending today. With ~250 weekdays
    per year and a ~5-30s per-symbol scan run, expect 30-90 minutes
    per strategy on first invocation. The `signals` table is the input
    to ``stockscan ml train``, so backfilling is a prerequisite for
    fitting a meta-label model on a brand-new strategy.

    Examples:
        stockscan signals backfill donchian_trend
        stockscan signals backfill all --start 2024-01-01
        stockscan signals backfill rsi2_meanrev --every 5    # weekly Wed scans
    """
    from datetime import date as _date
    from datetime import timedelta as _td

    from rich.progress import (
        BarColumn,
        Progress,
        SpinnerColumn,
        TextColumn,
    )
    from sqlalchemy import text

    from stockscan.db import session_scope
    from stockscan.scan import ScanRunner

    discover_strategies()

    # Resolve target strategies
    if strategy == "all":
        targets = STRATEGY_REGISTRY.names()
    elif strategy in STRATEGY_REGISTRY.names():
        targets = [strategy]
    else:
        console.print(
            f"[red]✗[/red] Unknown strategy {strategy!r}. "
            f"Registered: {', '.join(STRATEGY_REGISTRY.names())}"
        )
        raise typer.Exit(1)

    # Resolve date range. Default: 1 year daily.
    end_d = _date.fromisoformat(end) if end else _date.today()
    start_d = _date.fromisoformat(start) if start else (end_d - _td(days=365))
    if start_d > end_d:
        console.print(
            f"[red]✗[/red] --start ({start_d}) is after --end ({end_d})."
        )
        raise typer.Exit(1)

    # Build the candidate date list: weekdays only, every Nth.
    candidates: list[_date] = []
    cursor = start_d
    counter = 0
    while cursor <= end_d:
        if cursor.weekday() < 5:  # Mon-Fri
            if counter % every == 0:
                candidates.append(cursor)
            counter += 1
        cursor += _td(days=1)

    if not candidates:
        console.print("[yellow]No weekdays in the requested range.[/yellow]")
        return

    runner = ScanRunner()

    for tgt in targets:
        # Resolve the strategy's CURRENT version. Skipping is keyed on
        # (strategy_name, strategy_version) so that bumping a strategy's
        # version invalidates prior scans and they get re-run automatically
        # — without --force AND without duplicating already-current dates.
        # Older-version rows stay in the DB intentionally for comparison;
        # use ``stockscan signals delete`` to remove them when desired.
        try:
            current_version = STRATEGY_REGISTRY.get(tgt).version
        except KeyError:
            console.print(f"[red]✗[/red] Strategy {tgt!r} not registered.")
            continue

        if not force:
            with session_scope() as s:
                existing_rows = s.execute(
                    text(
                        """
                        SELECT as_of_date FROM strategy_runs
                        WHERE strategy_name = :n
                          AND strategy_version = :v
                          AND as_of_date BETWEEN :s AND :e
                        """
                    ),
                    {"n": tgt, "v": current_version, "s": start_d, "e": end_d},
                ).all()
                existing = {r[0] for r in existing_rows}
        else:
            existing = set()

        to_run = [d for d in candidates if d not in existing]
        skipped = len(candidates) - len(to_run)

        console.print(
            f"[cyan]→[/cyan] [bold]{tgt}[/bold] v{current_version}: "
            f"{len(to_run)} dates to scan "
            f"(skipping {skipped} at current version, range {start_d}..{end_d})"
        )

        if not to_run:
            console.print(f"  [green]✓[/green] {tgt} already complete in range")
            continue

        # Rich progress bar for the inner loop. Each iteration is one
        # scan run (per-strategy, per-date), which itself loops the
        # universe — so this is a coarse-grained progress indicator.
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total}"),
            TextColumn("•"),
            TextColumn("[cyan]{task.fields[stats]}[/cyan]"),
            console=console,
        ) as progress:
            task = progress.add_task(
                f"{tgt}", total=len(to_run), stats="0 signals"
            )
            total_signals = 0
            failures = 0
            for as_of in to_run:
                try:
                    summary = runner.run(tgt, as_of)
                    total_signals += summary.signals_emitted
                except Exception as exc:  # soft-fail per date
                    failures += 1
                    log.warning(
                        "signals backfill: %s @ %s failed: %s", tgt, as_of, exc
                    )
                progress.update(
                    task,
                    advance=1,
                    stats=f"{total_signals} signals"
                    + (f" · {failures} failed" if failures else ""),
                )

        console.print(
            f"  [green]✓[/green] {tgt} done: {total_signals} signals across "
            f"{len(to_run) - failures} successful scans"
            + (f" ({failures} failed)" if failures else "")
        )


@signals_app.command("delete")
def signals_delete_cmd(
    strategy: str = typer.Option(
        ..., "--strategy", "-s", help="Strategy name to delete signals for"
    ),
    version: str = typer.Option(
        ...,
        "--version",
        "-v",
        help=(
            "Strategy version to delete (e.g. '1.0.0'). Required — this command "
            "is intentionally explicit so you can't accidentally wipe the "
            "current version's signals."
        ),
    ),
    start: str | None = typer.Option(
        None,
        "--start",
        help="ISO start date (inclusive). Default: no lower bound.",
    ),
    end: str | None = typer.Option(
        None,
        "--end",
        help="ISO end date (inclusive). Default: no upper bound.",
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the interactive confirmation prompt."
    ),
) -> None:
    """Delete signals + strategy_runs for a specific (strategy, version).

    Use this to clean up data from a prior strategy version after a
    backfill under the new version. The signal-detail page, signals
    list, dashboard, and meta-label trainer all default to the
    current registered version, so older-version signals are inert
    once the strategy has been bumped — but they still occupy space
    and can complicate ad-hoc analytics queries. This command is the
    explicit way to remove them.

    The deletion respects the foreign-key relationship: signals are
    deleted first, then their parent strategy_runs rows. Both counts
    are reported.

    Examples:
        stockscan signals delete --strategy donchian_trend --version 1.0.0
        stockscan signals delete -s donchian_trend -v 1.0.0 --start 2020-01-01 --end 2024-12-31
        stockscan signals delete -s rsi2_meanrev -v 1.0.0 --yes      # script-friendly
    """
    from datetime import date as _date

    from sqlalchemy import text

    from stockscan.db import session_scope

    discover_strategies()

    # Build optional date-range params.
    params: dict[str, object] = {"n": strategy, "v": version}
    date_clause = ""
    if start:
        params["s"] = _date.fromisoformat(start)
        date_clause += " AND as_of_date >= :s"
    if end:
        params["e"] = _date.fromisoformat(end)
        date_clause += " AND as_of_date <= :e"

    # Count what would be deleted before doing anything destructive.
    with session_scope() as sess:
        run_count = sess.execute(
            text(
                f"""
                SELECT COUNT(*) FROM strategy_runs
                WHERE strategy_name = :n
                  AND strategy_version = :v
                  {date_clause}
                """
            ),
            params,
        ).scalar_one()
        signal_count = sess.execute(
            text(
                f"""
                SELECT COUNT(*) FROM signals
                WHERE strategy_name = :n
                  AND strategy_version = :v
                  {date_clause}
                """
            ),
            params,
        ).scalar_one()

    if run_count == 0 and signal_count == 0:
        console.print(
            f"[yellow]Nothing to delete[/yellow] for "
            f"{strategy!r} v{version}"
            + (f" in [{start or '*'}..{end or '*'}]" if start or end else "")
            + "."
        )
        return

    console.print(
        f"[bold]Will delete[/bold]: {signal_count:,} signals + "
        f"{run_count:,} strategy_runs for "
        f"[cyan]{strategy}[/cyan] [magenta]v{version}[/magenta]"
        + (f" in [{start or '*'}..{end or '*'}]" if start or end else "")
        + "."
    )

    if not yes:
        confirm = typer.confirm(
            "Proceed? This is permanent.", default=False
        )
        if not confirm:
            console.print("[yellow]Aborted.[/yellow]")
            raise typer.Exit(1)

    # Delete signals first to respect the FK from signals.run_id ->
    # strategy_runs.run_id (no ON DELETE CASCADE in 0001_initial_schema).
    with session_scope() as sess:
        sess.execute(
            text(
                f"""
                DELETE FROM signals
                WHERE strategy_name = :n
                  AND strategy_version = :v
                  {date_clause}
                """
            ),
            params,
        )
        sess.execute(
            text(
                f"""
                DELETE FROM strategy_runs
                WHERE strategy_name = :n
                  AND strategy_version = :v
                  {date_clause}
                """
            ),
            params,
        )

    console.print(
        f"[green]✓[/green] Deleted {signal_count:,} signals + "
        f"{run_count:,} strategy_runs."
    )


# ----------------------------------------------------------------------
# Analysis commands
# ----------------------------------------------------------------------
@analysis_app.command("run")
def analysis_run_cmd(
    symbol: str | None = typer.Option(
        None, "--symbol", "-s", help="Single ticker to analyze"
    ),
    all_watched: bool = typer.Option(
        False, "--all-watched", help="Analyze every symbol on the watchlist"
    ),
) -> None:
    """Generate technical analysis for one symbol or every watched name.

    Outputs a Rich table summarizing the trend, volatility, expected
    7d/30d ranges, and a few of the curated options-context observations.
    Doesn't render charts (those live on the web UI).
    """
    from stockscan.analysis import analyze_symbol, analyze_watchlist

    if not symbol and not all_watched:
        console.print(
            "[red]✗[/red] Specify either --symbol TICKER or --all-watched."
        )
        raise typer.Exit(1)

    if symbol:
        analyses = [analyze_symbol(symbol.upper().strip())]
    else:
        analyses = analyze_watchlist()

    if not analyses:
        console.print("[yellow]No analyses generated (empty watchlist?).[/yellow]")
        return

    for a in analyses:
        if not a.available:
            console.print(
                f"[yellow]⚠ {a.symbol}: unavailable[/yellow]"
                + (f" — {a.failures[0]}" if a.failures else "")
            )
            continue
        table = Table(title=f"Analysis: {a.symbol} (as of {a.as_of})")
        table.add_column("Field", style="cyan")
        table.add_column("Value", justify="right")
        if a.last_close:
            table.add_row("Last close", f"${a.last_close:.2f}")
        if a.trend.available:
            table.add_row("Trend", a.trend.label)
        if a.volatility.available:
            table.add_row("Volatility regime", a.volatility.label)
            if a.volatility.realized_vol_21d_pct is not None:
                table.add_row("HV (21d annualised)", f"{a.volatility.realized_vol_21d_pct:.1f}%")
            if a.volatility.hv_percentile is not None:
                table.add_row("HV percentile (1y)", f"{a.volatility.hv_percentile:.0f}%")
            if a.volatility.expected_7d:
                er = a.volatility.expected_7d
                table.add_row(
                    "7-day ±1σ",
                    f"${er.low:.2f}–${er.high:.2f} (±{er.sigma_pct:.1f}%)",
                )
            if a.volatility.expected_30d:
                er = a.volatility.expected_30d
                table.add_row(
                    "30-day ±1σ",
                    f"${er.low:.2f}–${er.high:.2f} (±{er.sigma_pct:.1f}%)",
                )
        if a.momentum.available:
            if a.momentum.rsi_14 is not None:
                table.add_row("RSI(14)", f"{a.momentum.rsi_14:.1f} ({a.momentum.rsi_label})")
            if a.momentum.macd_line is not None:
                table.add_row("MACD state", a.momentum.macd_label)
        if a.options_context.nearest_support:
            ns = a.options_context.nearest_support
            table.add_row(
                "Nearest support",
                f"${ns.price:.2f} ({a.options_context.pct_to_support:.2f}% below)",
            )
        if a.options_context.nearest_resistance:
            nr = a.options_context.nearest_resistance
            table.add_row(
                "Nearest resistance",
                f"${nr.price:.2f} ({a.options_context.pct_to_resistance:.2f}% above)",
            )
        if a.options_context.days_to_earnings is not None:
            table.add_row(
                "Days to earnings",
                f"{a.options_context.days_to_earnings}",
            )
        console.print(table)
        if a.options_context.observations:
            console.print("[bold cyan]Observations:[/bold cyan]")
            for obs in a.options_context.observations:
                console.print(f"  • {obs}")
            console.print()


def main() -> None:
    """Entry point used by `python -m stockscan`."""
    app()


if __name__ == "__main__":
    main()
