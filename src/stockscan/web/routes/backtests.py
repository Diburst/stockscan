"""Backtests pages — list + detail with equity curve, per-symbol price chart, and trade markers.

Endpoints:
  GET  /backtests               — run form + job status + recent runs
  POST /backtests/run           — start a background backtest job
  GET  /backtests/run/status    — polling fragment for the running job
  GET  /backtests/{run_id}      — single-run report
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from fastapi import APIRouter, Depends, Form, Query, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from stockscan.backtest.job import (
    DEFAULT_CAPITAL,
    DEFAULT_COMMISSION,
    DEFAULT_SLIPPAGE_BPS,
    DEFAULT_WINDOW_DAYS,
    consume_finished as consume_backtest_job,
    current_job as current_backtest_job,
    start_backtest,
)
from stockscan.backtest.store import list_runs
from stockscan.data.store import get_bars
from stockscan.strategies import STRATEGY_REGISTRY, discover_strategies
from stockscan.web.deps import attach_hx_toast, flash_redirect, get_session, render

router = APIRouter(prefix="/backtests")
log = logging.getLogger(__name__)


def _form_ctx() -> dict[str, object]:
    """Template context for ``backtests/_run_form.html`` — strategy choices
    and the CLI's defaults so the form and ``backtest run`` agree."""
    discover_strategies()
    today = date.today()
    return {
        "strategies": STRATEGY_REGISTRY.all(),
        "default_start": (today - timedelta(days=DEFAULT_WINDOW_DAYS)).isoformat(),
        "default_end": today.isoformat(),
        "default_capital": int(DEFAULT_CAPITAL),
        "default_slippage_bps": DEFAULT_SLIPPAGE_BPS,
        "default_commission": DEFAULT_COMMISSION,
    }


def _status_ctx(*, message: str | None = None) -> dict[str, object]:
    """Template context for ``backtests/_run_status.html``.

    While a job runs the fragment carries the running job and re-polls;
    once finished the job is consumed here so the result line renders once.
    """
    job = current_backtest_job()
    if job is not None and job.status == "running":
        return {"job": job, "finished": None, "message": message}
    return {"job": None, "finished": consume_backtest_job(), "message": message}


def _parse_date(value: str | None) -> date | None:
    """ISO date from a form field; '' means "use the CLI default"."""
    if value is None or value.strip() == "":
        return None
    return date.fromisoformat(value.strip())


def _parse_symbols(value: str | None) -> list[str] | None:
    """Free-text symbols — whitespace/comma separated, upper-cased, de-duped
    in order. Empty → None (point-in-time S&P 500 membership)."""
    if not value:
        return None
    seen: dict[str, None] = {}
    for tok in value.replace(",", " ").split():
        seen.setdefault(tok.upper(), None)
    return list(seen) or None


@router.get("")
def backtests_list(request: Request):
    """Run form, job status and the 100 most recent backtest runs."""
    runs = list_runs(limit=100)
    return render(
        request, "backtests/list.html", runs=runs, **_form_ctx(), **_status_ctx(),
    )


@router.post("/run")
def backtest_run(
    request: Request,
    strategy: str = Form(...),
    start: str | None = Form(None),
    end: str | None = Form(None),
    capital: float = Form(DEFAULT_CAPITAL),
    slippage_bps: float = Form(DEFAULT_SLIPPAGE_BPS),
    commission: float = Form(DEFAULT_COMMISSION),
    symbols: str | None = Form(None),
    note: str | None = Form(None),
):
    """Start a backtest as a BACKGROUND job with the same inputs as
    ``stockscan backtest run``; blank dates take the CLI defaults.

    HTMX callers get the ``_run_status.html`` fragment (which polls
    ``GET /backtests/run/status`` every 2 s); a plain form POST redirects
    back to the list with a flash. Single-flight: a second POST while a job
    runs is told so instead of starting another.
    """
    is_hx = request.headers.get("HX-Request") == "true"

    def _respond(kind: str, message: str):
        if not is_hx:
            return flash_redirect("/backtests", kind, message)
        response = render(request, "backtests/_run_status.html", **_status_ctx())
        return attach_hx_toast(response, kind, message)

    existing = current_backtest_job()
    if existing is not None and existing.status == "running":
        return _respond("info", "A backtest is already running — wait for it to finish")

    try:
        start_d = _parse_date(start)
        end_d = _parse_date(end)
    except ValueError:
        return _respond("error", "Dates must be ISO (YYYY-MM-DD)")
    if start_d and end_d and start_d >= end_d:
        return _respond("error", "Start date must be before end date")
    if capital <= 0:
        return _respond("error", "Starting capital must be positive")

    try:
        _job, started_new = start_backtest(
            strategy=strategy,
            start=start_d,
            end=end_d,
            capital=capital,
            slippage_bps=slippage_bps,
            commission=commission,
            symbols=_parse_symbols(symbols),
            note=(note or "").strip() or None,
        )
    except KeyError:
        return _respond("error", f"Unknown strategy: {strategy}")
    if not started_new:
        return _respond("info", "A backtest is already running — wait for it to finish")
    return _respond("info", f"Backtest started — {strategy}, runs in the background")


@router.get("/run/status")
def backtest_run_status(request: Request):
    """Polling endpoint for the background backtest.

    While the job runs: the self-replacing status fragment (it re-polls
    every 2 s). When it finishes: the same fragment with a link to the
    saved report, or the error; the finished job is consumed so a stray
    later poll doesn't re-announce it.
    """
    ctx = _status_ctx()
    response = render(request, "backtests/_run_status.html", **ctx)
    finished = ctx["finished"]
    if finished is None:
        return response
    if finished.error:
        return attach_hx_toast(response, "error", "Backtest failed")
    return attach_hx_toast(response, "success", f"Backtest saved — run #{finished.run_id}")


@router.get("/{run_id}")
def backtest_detail(
    run_id: int,
    request: Request,
    symbol: str | None = Query(None, description="Selected symbol for the price chart"),
    s: Session = Depends(get_session),
):
    """Single-run view: summary metrics, equity curve, trade table, and a
    per-symbol price chart with entry/exit markers. ``?symbol=`` picks the
    charted symbol (defaults to the most-traded one); an unknown run_id
    renders the empty-state page."""
    run_row = s.execute(
        text(
            """
            SELECT run_id, strategy_name, strategy_version, params_json,
                   start_date, end_date, starting_capital, ending_equity,
                   num_trades, metrics_json, note, created_at
            FROM backtest_runs WHERE run_id = :rid
            """
        ),
        {"rid": run_id},
    ).first()
    if not run_row:
        return render(
            request, "backtests/detail.html",
            run=None, trades=[], equity=[],
            symbols=[], selected_symbol=None, chart_bars=[], chart_markers=[],
            avg_r=None, best_r=None, worst_r=None,
        )

    trade_rows = s.execute(
        text(
            """
            SELECT symbol, qty, entry_date, entry_price, stop_price,
                   exit_date, exit_price, exit_reason,
                   realized_pnl, return_pct, r_multiple, holding_days,
                   entry_metadata
            FROM backtest_trades WHERE run_id = :rid
            ORDER BY entry_date
            """
        ),
        {"rid": run_id},
    ).all()

    equity_rows = s.execute(
        text(
            """
            SELECT as_of_date, total_equity, cash, positions_value, num_open
            FROM backtest_equity_curve WHERE run_id = :rid
            ORDER BY as_of_date
            """
        ),
        {"rid": run_id},
    ).all()

    # Aggregate R-multiple stats across this run's trades.
    r_values = [float(t.r_multiple) for t in trade_rows if t.r_multiple is not None]
    avg_r = sum(r_values) / len(r_values) if r_values else None
    best_r = max(r_values) if r_values else None
    worst_r = min(r_values) if r_values else None

    # Distinct symbols traded in this run, ordered by trade count desc.
    symbol_counts: dict[str, int] = {}
    for t in trade_rows:
        symbol_counts[t.symbol] = symbol_counts.get(t.symbol, 0) + 1
    symbols = sorted(symbol_counts.items(), key=lambda kv: (-kv[1], kv[0]))
    symbol_list = [sym for sym, _ in symbols]

    # Pick a symbol for the chart: explicit query param, else most-traded.
    selected_symbol: str | None = None
    if symbol and symbol in symbol_counts:
        selected_symbol = symbol
    elif symbol_list:
        selected_symbol = symbol_list[0]

    # Load bars for the selected symbol over the run window.
    chart_bars: list[dict] = []
    chart_markers: list[dict] = []
    selected_trades: list = []
    if selected_symbol is not None:
        # Pull bars from a few days before start to a few days after end so
        # the chart has padding around the first/last trade markers.
        start_dt = datetime.combine(run_row.start_date, datetime.min.time(), tzinfo=timezone.utc)
        end_dt = datetime.combine(run_row.end_date, datetime.max.time(), tzinfo=timezone.utc)
        try:
            bars_df = get_bars(selected_symbol, start_dt - timedelta(days=10),
                               end_dt + timedelta(days=10), session=s)
        except Exception:
            bars_df = None

        if bars_df is not None and not bars_df.empty:
            for ts, row in bars_df.iterrows():
                chart_bars.append({
                    "time": ts.date().isoformat(),
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    # Volume rides along with each bar so the chart can render a
                    # histogram in the bottom panel; soft-fail if the column is
                    # missing (some test fixtures don't include it).
                    "volume": float(row["volume"]) if "volume" in row and row["volume"] is not None else 0.0,
                })

        # Trades for the selected symbol; build chart markers from them.
        for t in trade_rows:
            if t.symbol != selected_symbol:
                continue
            selected_trades.append(t)
            # Entry marker (green up-arrow below the bar)
            chart_markers.append({
                "time": t.entry_date.isoformat(),
                "position": "belowBar",
                "color": "#059669",   # ok-600
                "shape": "arrowUp",
                "text": f"Entry @ ${float(t.entry_price):.2f}",
            })
            # Exit marker (red down-arrow above the bar) if exited
            if t.exit_date and t.exit_price is not None:
                color = "#059669" if (t.r_multiple or 0) > 0 else "#dc2626"
                r_label = f" ({float(t.r_multiple):+.2f}R)" if t.r_multiple is not None else ""
                chart_markers.append({
                    "time": t.exit_date.isoformat(),
                    "position": "aboveBar",
                    "color": color,
                    "shape": "arrowDown",
                    "text": f"Exit: {t.exit_reason or '—'} @ ${float(t.exit_price):.2f}{r_label}",
                })

    return render(
        request,
        "backtests/detail.html",
        run=run_row,
        trades=trade_rows,
        equity=equity_rows,
        avg_r=avg_r,
        best_r=best_r,
        worst_r=worst_r,
        # Per-symbol chart bits
        symbols=symbols,                  # [(symbol, trade_count), ...]
        selected_symbol=selected_symbol,
        chart_bars=chart_bars,
        chart_markers=chart_markers,
        selected_trades=selected_trades,
    )
