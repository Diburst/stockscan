"""Backtests pages — list + detail with equity curve, per-symbol price chart, and trade markers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from stockscan.backtest.store import list_runs
from stockscan.data.store import get_bars
from stockscan.web.deps import get_session, render

router = APIRouter(prefix="/backtests")


@router.get("")
async def backtests_list(request: Request):
    runs = list_runs(limit=100)
    return render(request, "backtests/list.html", runs=runs)


@router.get("/{run_id}")
async def backtest_detail(
    run_id: int,
    request: Request,
    symbol: str | None = Query(None, description="Selected symbol for the price chart"),
    s: Session = Depends(get_session),
):
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
