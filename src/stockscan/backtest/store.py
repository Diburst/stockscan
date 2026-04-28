"""Persist backtest runs and trades to the backtest_* tables.

Idempotent on `params_hash` for a given (strategy_name, strategy_version,
start_date, end_date, starting_capital) — re-running an identical config
returns the existing run_id rather than creating a duplicate.
"""

from __future__ import annotations

import json
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

from stockscan.backtest.engine import BacktestResult
from stockscan.db import session_scope


def save_run(result: BacktestResult, *, note: str | None = None) -> int:
    """Persist a BacktestResult; returns the run_id."""
    cfg = result.config
    params_json = cfg.params.model_dump(mode="json")
    params_hash = cfg.strategy_cls.hash_params(cfg.params)

    metrics_json = result.report.to_dict()
    ending_equity = (
        Decimal(str(float(result.equity_curve.iloc[-1])))
        if not result.equity_curve.empty
        else cfg.starting_capital
    )

    insert_run = text(
        """
        INSERT INTO backtest_runs
            (strategy_name, strategy_version, params_json, params_hash,
             start_date, end_date, starting_capital, ending_equity,
             num_trades, metrics_json, note)
        VALUES
            (:strategy_name, :strategy_version, CAST(:params_json AS JSONB), :params_hash,
             :start_date, :end_date, :starting_capital, :ending_equity,
             :num_trades, CAST(:metrics_json AS JSONB), :note)
        RETURNING run_id;
        """
    )

    with session_scope() as s:
        row = s.execute(
            insert_run,
            {
                "strategy_name": cfg.strategy_cls.name,
                "strategy_version": cfg.strategy_cls.version,
                "params_json": json.dumps(params_json),
                "params_hash": params_hash,
                "start_date": cfg.start_date,
                "end_date": cfg.end_date,
                "starting_capital": cfg.starting_capital,
                "ending_equity": ending_equity,
                "num_trades": len(result.trades),
                "metrics_json": json.dumps(metrics_json),
                "note": note,
            },
        ).one()
        run_id = int(row.run_id)

        _save_trades(s, run_id, result)
        _save_equity_curve(s, run_id, result)

    return run_id


def _save_trades(session: Session, run_id: int, result: BacktestResult) -> None:
    if not result.trades:
        return
    sql = text(
        """
        INSERT INTO backtest_trades
            (run_id, symbol, side, qty, entry_date, entry_price,
             exit_date, exit_price, exit_reason, commission,
             realized_pnl, return_pct, holding_days,
             stop_price, r_multiple, entry_metadata)
        VALUES
            (:run_id, :symbol, 'long', :qty, :entry_date, :entry_price,
             :exit_date, :exit_price, :exit_reason, :commission,
             :realized_pnl, :return_pct, :holding_days,
             :stop_price, :r_multiple, CAST(:entry_metadata AS JSONB))
        """
    )
    payload = [
        {
            "run_id": run_id,
            "symbol": t.symbol,
            "qty": t.qty,
            "entry_date": t.entry_date,
            "entry_price": t.entry_price,
            "exit_date": t.exit_date,
            "exit_price": t.exit_price,
            "exit_reason": t.exit_reason or "unspecified",
            "commission": t.commission,
            "realized_pnl": t.pnl,
            "return_pct": t.return_pct,
            "holding_days": t.holding_days,
            "stop_price": t.entry_stop,
            "r_multiple": t.r_multiple,
            "entry_metadata": (
                json.dumps(t.entry_metadata, default=str) if t.entry_metadata else None
            ),
        }
        for t in result.trades
    ]
    session.execute(sql, payload)


def _save_equity_curve(session: Session, run_id: int, result: BacktestResult) -> None:
    if result.equity_curve.empty:
        return
    sql = text(
        """
        INSERT INTO backtest_equity_curve
            (run_id, as_of_date, cash, positions_value, total_equity, high_water_mark, num_open)
        VALUES
            (:run_id, :as_of_date, :cash, :positions_value, :total_equity, :hwm, :num_open)
        """
    )
    hwm = Decimal(0)
    payload = []
    for ts, equity in result.equity_curve.items():
        d = ts.date() if hasattr(ts, "date") else ts
        eq = Decimal(str(float(equity)))
        hwm = max(hwm, eq)
        pos_val = (
            Decimal(str(float(result.positions_value.loc[ts])))
            if ts in result.positions_value.index
            else Decimal(0)
        )
        payload.append(
            {
                "run_id": run_id,
                "as_of_date": d,
                "cash": eq - pos_val,
                "positions_value": pos_val,
                "total_equity": eq,
                "hwm": hwm,
                "num_open": 0,  # filled in v1.5 from per-day position state
            }
        )
    session.execute(sql, payload)


def list_runs(
    strategy_name: str | None = None, *, limit: int = 50
) -> list[dict[str, object]]:
    """Recent backtest runs, optionally filtered by strategy."""
    if strategy_name:
        sql = text(
            """
            SELECT run_id, strategy_name, strategy_version, start_date, end_date,
                   num_trades, ending_equity, metrics_json, created_at
            FROM backtest_runs
            WHERE strategy_name = :strategy
            ORDER BY created_at DESC
            LIMIT :limit
            """
        )
        params = {"strategy": strategy_name, "limit": limit}
    else:
        sql = text(
            """
            SELECT run_id, strategy_name, strategy_version, start_date, end_date,
                   num_trades, ending_equity, metrics_json, created_at
            FROM backtest_runs
            ORDER BY created_at DESC
            LIMIT :limit
            """
        )
        params = {"limit": limit}
    with session_scope() as s:
        return [dict(r._mapping) for r in s.execute(sql, params)]
