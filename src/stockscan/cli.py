"""`stockscan` command-line interface.

Phase 0 commands:
  stockscan health                    — DB + provider connectivity
  stockscan db migrate                — apply Alembic migrations
  stockscan db status                 — current schema revision
  stockscan refresh universe          — pull S&P 500 membership from EODHD
  stockscan refresh bars [SYMBOL...]  — backfill bars
  stockscan refresh macro [SERIES...] — backfill FRED macro series (HY OAS)
  stockscan strategies list           — print registered strategies
  stockscan strategies show NAME      — print strategy metadata + params schema

Phase 1+ will extend this with: scan run, backtest run, trade list, etc.
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
app.add_typer(db_app, name="db")
app.add_typer(refresh_app, name="refresh")
app.add_typer(strat_app, name="strategies")
app.add_typer(backtest_app, name="backtest")
app.add_typer(scan_app, name="scan")
app.add_typer(jobs_app, name="jobs")
app.add_typer(watchlist_app, name="watchlist")
app.add_typer(technical_app, name="technical")


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
        None, help="Symbols to refresh; default = ALL members ever"
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
    """Backfill / incrementally update bars.

    Default behavior fetches bars for **every symbol ever in the S&P 500**,
    so backtests of historical periods have data for companies that have
    since been removed (survivorship-bias correction).

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


def main() -> None:
    """Entry point used by `python -m stockscan`."""
    app()


if __name__ == "__main__":
    main()
