"""Backtest result exporter — one JSON dump with everything to review a run.

The goal is "hand this file to a reviewer (or a Claude session) and they have
full context to evaluate decisions and propose tuning." Sections:

  run                — the backtest_runs row, with metrics expanded.
  summary_stats      — derived from trades: win rate, avg R, exit-reason mix,
                       hold-time distribution, and for every numeric key the
                       strategy wrote into entry_metadata, its mean on winners
                       vs losers. The hot table to read first.
  trades             — every backtest_trades row, with the strategy's own
                       entry_metadata as written by ``signals()``.
  equity_curve       — daily total equity + high-water mark.
  regime_overlay     — daily market regime (label, trend gate, vol scalar,
                       credit-stress flag). Useful for explaining clusters of
                       wins or losses tied to regime transitions.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pandas as pd
from sqlalchemy import text
from sqlalchemy.orm import Session

from stockscan.db import session_scope


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def export_run(
    run_id: int,
    *,
    include_regime: bool = True,
    session: Session | None = None,
) -> dict[str, Any]:
    """Build a self-contained review dict for one backtest run.

    The dict is JSON-serialisable (Decimals become strings, dates become ISO
    strings). Pass the result to ``json.dumps(..., default=str, indent=2)``.

    Parameters
    ----------
    run_id
        ``backtest_runs.run_id``.
    include_regime
        Include the daily regime overlay across the run window.
    session
        Reuse an existing session; otherwise the function opens its own.
    """
    if session is not None:
        return _export_with_session(session, run_id, include_regime)
    with session_scope() as s:
        return _export_with_session(s, run_id, include_regime)


def _export_with_session(s: Session, run_id: int, include_regime: bool) -> dict[str, Any]:
    run_row = _load_run(s, run_id)
    trades = _load_trades(s, run_id)
    equity = _load_equity(s, run_id)

    payload: dict[str, Any] = {
        "schema_version": 1,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "run": run_row,
        "summary_stats": _summary_stats(trades),
        "trades": trades,
        "equity_curve": equity,
    }

    if include_regime:
        payload["regime_overlay"] = _regime_overlay(
            s, run_row["start_date"], run_row["end_date"]
        )
    return payload


# ---------------------------------------------------------------------------
# Section loaders
# ---------------------------------------------------------------------------
def _load_run(s: Session, run_id: int) -> dict[str, Any]:
    row = s.execute(
        text(
            """
            SELECT run_id, strategy_name, strategy_version, params_json,
                   params_hash, start_date, end_date, starting_capital,
                   ending_equity, num_trades, metrics_json, created_at, note
            FROM backtest_runs WHERE run_id = :rid
            """
        ),
        {"rid": run_id},
    ).first()
    if row is None:
        raise LookupError(f"backtest run {run_id} not found")
    return {
        "run_id": int(row.run_id),
        "strategy_name": row.strategy_name,
        "strategy_version": row.strategy_version,
        "params_json": row.params_json,
        "params_hash": row.params_hash,
        "start_date": _iso(row.start_date),
        "end_date": _iso(row.end_date),
        "starting_capital": _dec(row.starting_capital),
        "ending_equity": _dec(row.ending_equity),
        "num_trades": int(row.num_trades) if row.num_trades is not None else None,
        "metrics": row.metrics_json or {},
        "note": row.note,
        "created_at": _iso(row.created_at),
    }


def _load_trades(s: Session, run_id: int) -> list[dict[str, Any]]:
    rows = s.execute(
        text(
            """
            SELECT trade_id, symbol, side, qty,
                   entry_date, entry_price, stop_price,
                   exit_date, exit_price, exit_reason,
                   commission, slippage,
                   realized_pnl, return_pct, r_multiple, holding_days,
                   mfe_pct, mae_pct,
                   entry_metadata
            FROM backtest_trades
            WHERE run_id = :rid
            ORDER BY entry_date, symbol, trade_id
            """
        ),
        {"rid": run_id},
    ).all()
    return [
        {
            "trade_id": int(r.trade_id),
            "symbol": r.symbol,
            "side": r.side,
            "qty": int(r.qty),
            "entry_date": _iso(r.entry_date),
            "entry_price": _dec(r.entry_price),
            "stop_price": _dec(r.stop_price),
            "exit_date": _iso(r.exit_date),
            "exit_price": _dec(r.exit_price),
            "exit_reason": r.exit_reason,
            "commission": _dec(r.commission),
            "slippage": _dec(r.slippage),
            "realized_pnl": _dec(r.realized_pnl),
            "return_pct": _dec(r.return_pct),
            "r_multiple": _dec(r.r_multiple),
            "holding_days": int(r.holding_days) if r.holding_days is not None else None,
            "mfe_pct": _dec(r.mfe_pct),
            "mae_pct": _dec(r.mae_pct),
            # entry_metadata is JSONB → already a dict, exactly as the
            # strategy's signals() wrote it.
            "entry_metadata": r.entry_metadata or {},
        }
        for r in rows
    ]


def _load_equity(s: Session, run_id: int) -> list[dict[str, Any]]:
    rows = s.execute(
        text(
            """
            SELECT as_of_date, cash, positions_value, total_equity,
                   high_water_mark, num_open
            FROM backtest_equity_curve
            WHERE run_id = :rid
            ORDER BY as_of_date
            """
        ),
        {"rid": run_id},
    ).all()
    return [
        {
            "date": _iso(r.as_of_date),
            "cash": _dec(r.cash),
            "positions_value": _dec(r.positions_value),
            "total_equity": _dec(r.total_equity),
            "high_water_mark": _dec(r.high_water_mark),
            "num_open": int(r.num_open),
        }
        for r in rows
    ]


def _regime_overlay(s: Session, start: str | date, end: str | date) -> list[dict[str, Any]]:
    rows = s.execute(
        text(
            """
            SELECT as_of_date, regime, trend_gate_open, vol_scalar, credit_stress_flag
            FROM market_regime
            WHERE as_of_date BETWEEN :start AND :end
            ORDER BY as_of_date
            """
        ),
        {"start": start, "end": end},
    ).all()
    return [
        {
            "date": _iso(r.as_of_date),
            "regime": r.regime,
            "trend_gate_open": bool(r.trend_gate_open),
            "vol_scalar": _dec(r.vol_scalar),
            "credit_stress_flag": bool(r.credit_stress_flag),
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Summary statistics
# ---------------------------------------------------------------------------
def _summary_stats(trades: list[dict[str, Any]]) -> dict[str, Any]:
    """Derive the table a reviewer reads first: counts, win rate, R-multiple
    distribution, exit-reason mix, and per-metadata-key averages split by
    winners vs losers."""
    if not trades:
        return {"n_trades": 0}

    closed = [t for t in trades if t.get("exit_date")]
    pnls = [_as_float(t.get("realized_pnl")) for t in closed]
    rs   = [_as_float(t.get("r_multiple"))   for t in closed if t.get("r_multiple") is not None]
    holds = [t["holding_days"] for t in closed if t.get("holding_days") is not None]
    rets = [_as_float(t.get("return_pct")) for t in closed]

    winners = [t for t in closed if _as_float(t.get("realized_pnl")) > 0]
    losers  = [t for t in closed if _as_float(t.get("realized_pnl")) <= 0]

    exit_mix: dict[str, int] = {}
    for t in closed:
        k = t.get("exit_reason") or "unspecified"
        exit_mix[k] = exit_mix.get(k, 0) + 1

    def _q(xs: list[float], p: float) -> float | None:
        if not xs:
            return None
        return float(pd.Series(xs).quantile(p))

    summary: dict[str, Any] = {
        "n_trades": len(trades),
        "n_closed": len(closed),
        "n_winners": len(winners),
        "n_losers":  len(losers),
        "win_rate":  round(len(winners) / len(closed), 4) if closed else None,
        "exit_reason_mix": exit_mix,
        "r_multiple": {
            "mean":   _round_or_none(sum(rs) / len(rs) if rs else None, 4),
            "median": _round_or_none(_q(rs, 0.5), 4),
            "p25":    _round_or_none(_q(rs, 0.25), 4),
            "p75":    _round_or_none(_q(rs, 0.75), 4),
            "min":    _round_or_none(min(rs) if rs else None, 4),
            "max":    _round_or_none(max(rs) if rs else None, 4),
        },
        "return_pct": {
            "mean":   _round_or_none(sum(rets) / len(rets) if rets else None, 4),
            "median": _round_or_none(_q(rets, 0.5), 4),
        },
        "holding_days": {
            "mean":   _round_or_none(sum(holds) / len(holds) if holds else None, 2),
            "median": _round_or_none(_q(holds, 0.5), 2),
            "max":    max(holds) if holds else None,
        },
        "total_realized_pnl": _round_or_none(sum(pnls) if pnls else None, 2),
        "entry_metadata": _metadata_breakdown(winners, losers),
    }

    # Best / worst named trades for quick reference.
    if rs:
        best  = max(closed, key=lambda t: _as_float(t.get("r_multiple")) or float("-inf"))
        worst = min(closed, key=lambda t: _as_float(t.get("r_multiple")) or float("inf"))
        summary["best_trade"]  = _trade_capsule(best)
        summary["worst_trade"] = _trade_capsule(worst)

    return summary


def _trade_capsule(t: dict[str, Any]) -> dict[str, Any]:
    return {
        "trade_id": t.get("trade_id"),
        "symbol": t.get("symbol"),
        "entry_date": t.get("entry_date"),
        "exit_date": t.get("exit_date"),
        "exit_reason": t.get("exit_reason"),
        "r_multiple": t.get("r_multiple"),
        "return_pct": t.get("return_pct"),
        "entry_metadata": t.get("entry_metadata") or {},
    }


def _metadata_breakdown(
    winners: list[dict[str, Any]], losers: list[dict[str, Any]]
) -> dict[str, Any]:
    """Average every numeric entry_metadata key across winners vs losers —
    answers "which of the strategy's own inputs looked different on the
    trades that worked vs the trades that didn't?". Keys are discovered from
    the trades themselves; None values are skipped."""
    def _numeric(trades: list[dict[str, Any]], key: str) -> list[float]:
        vals = []
        for t in trades:
            v = (t.get("entry_metadata") or {}).get(key)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                vals.append(float(v))
        return vals

    keys: list[str] = []
    for t in [*winners, *losers]:
        for k in (t.get("entry_metadata") or {}):
            if k not in keys:
                keys.append(k)

    out: dict[str, Any] = {}
    for k in keys:
        wv = _numeric(winners, k)
        lv = _numeric(losers, k)
        if not wv and not lv:
            continue
        wa = round(sum(wv) / len(wv), 4) if wv else None
        la = round(sum(lv) / len(lv), 4) if lv else None
        out[k] = {
            "winners_mean": wa, "winners_n": len(wv),
            "losers_mean":  la, "losers_n":  len(lv),
            "delta": None if wa is None or la is None else round(wa - la, 4),
        }
    return out


# ---------------------------------------------------------------------------
# Type / format helpers
# ---------------------------------------------------------------------------
def _dec(v: Any) -> str | None:
    """Render a Decimal-ish value as a string for lossless JSON serialisation."""
    if v is None:
        return None
    if isinstance(v, Decimal):
        return str(v)
    return str(v)


def _iso(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.isoformat(timespec="seconds")
    if isinstance(v, date):
        return v.isoformat()
    return str(v)


def _as_float(v: Any) -> float:
    if v is None:
        return 0.0
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return 0.0
    return float(v)


def _round_or_none(v: Any, places: int) -> float | None:
    if v is None:
        return None
    try:
        return round(float(v), places)
    except (TypeError, ValueError):
        return None
