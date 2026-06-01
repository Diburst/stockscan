"""Backtest result exporter — one JSON dump with everything to review a run.

The goal is "hand this file to a reviewer (or a Claude session) and they have
full context to evaluate decisions and propose tuning." Sections:

  run                — the backtest_runs row, with metrics expanded.
  summary_stats      — derived from trades: win rate, avg R, exit-reason mix,
                       per-input contribution averages on winners vs losers,
                       hold-time distribution. The hot table to read first.
  trades             — every backtest_trades row, with the strategy's own
                       entry_metadata (which carries the per-input score
                       breakdown for composite strategies).
  equity_curve       — daily total equity + high-water mark.
  per_day_scores     — for each symbol that traded, the strategy's reversal
                       score on every trading day in the run window. Lets a
                       reviewer see "near-miss" days that didn't enter and
                       compare entry days against the surrounding score
                       trajectory. Only populated when the strategy exposes a
                       reversal_score() method.
  regime_overlay     — daily market regime (label + composite score + sub-
                       scores). Useful for explaining clusters of wins or
                       losses tied to regime transitions.

The exporter degrades gracefully: a missing regime row, a symbol whose bars
have been refreshed, a strategy without a reversal_score method — all log a
note in the output rather than aborting.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pandas as pd
from sqlalchemy import text
from sqlalchemy.orm import Session

from stockscan.db import session_scope
from stockscan.strategies import STRATEGY_REGISTRY, discover_strategies

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def export_run(
    run_id: int,
    *,
    include_per_day: bool = True,
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
    include_per_day
        Compute and include the strategy's reversal_score for every trading
        day in the run window, for each symbol that traded. Adds runtime
        roughly proportional to (symbols × days × cost-per-score-call).
        Disable for a faster trade-only export.
    include_regime
        Include the daily regime overlay across the run window.
    session
        Reuse an existing session; otherwise the function opens its own.
    """
    if session is not None:
        return _export_with_session(session, run_id, include_per_day, include_regime)
    with session_scope() as s:
        return _export_with_session(s, run_id, include_per_day, include_regime)


def _export_with_session(
    s: Session, run_id: int, include_per_day: bool, include_regime: bool
) -> dict[str, Any]:
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

    if include_per_day:
        payload["per_day_scores"] = _per_day_scores(s, run_row, trades)
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
            # entry_metadata is JSONB → already a dict; carries score_breakdown
            # for strategies that produce one (reversal_swing).
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
            SELECT as_of_date, regime, composite_score,
                   vol_score, trend_score, breadth_score
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
            "composite_score": _dec(r.composite_score),
            "vol_score": _dec(r.vol_score),
            "trend_score": _dec(r.trend_score),
            "breadth_score": _dec(r.breadth_score),
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Per-day score recompute
# ---------------------------------------------------------------------------
@dataclass
class _PerDayContext:
    strategy_cls: type
    strategy: Any           # instance
    required: int           # required_history
    has_reversal_score: bool
    entry_threshold: float
    exit_threshold: float


def _per_day_scores(
    s: Session, run_row: dict[str, Any], trades: list[dict[str, Any]]
) -> dict[str, Any]:
    """For each symbol that traded, recompute the strategy's reversal score on
    every trading day in the run window. Skip cleanly when the strategy doesn't
    expose a reversal_score method or when a symbol's bars aren't available."""
    discover_strategies()
    try:
        cls = STRATEGY_REGISTRY.get(run_row["strategy_name"])
    except KeyError:
        return {
            "_note": f"strategy {run_row['strategy_name']!r} no longer registered — "
                     "skipping per-day score recompute.",
            "symbols": {},
        }
    strategy = cls()
    if not hasattr(strategy, "reversal_score"):
        return {
            "_note": f"strategy {cls.name!r} does not expose a reversal_score(view, as_of) "
                     "method — skipping per-day recompute. (See `backtest debug` for the "
                     "older per-symbol diagnostic.)",
            "symbols": {},
        }

    ctx = _PerDayContext(
        strategy_cls=cls,
        strategy=strategy,
        required=int(strategy.required_history()),
        has_reversal_score=True,
        entry_threshold=float(getattr(strategy, "entry_threshold", 0.25)),
        exit_threshold=float(getattr(strategy, "exit_threshold", 0.35)),
    )

    # Lazy import — get_bars may pull DB-bound providers; keep this off the
    # module-load path so import-time failures don't poison the simpler
    # trade-only export path.
    from stockscan.data.store import get_bars

    start_d = _parse_date(run_row["start_date"])
    end_d = _parse_date(run_row["end_date"])
    symbols = sorted({t["symbol"] for t in trades})

    out: dict[str, Any] = {
        "strategy": ctx.strategy_cls.name,
        "strategy_version": ctx.strategy_cls.version,
        "entry_threshold": ctx.entry_threshold,
        "exit_threshold": ctx.exit_threshold,
        "symbols": {},
        "_meta": {
            "n_symbols": len(symbols),
            "methodology_version": _safe_int(
                getattr(ctx.strategy_cls, "METHODOLOGY_VERSION", None)
            ),
        },
    }

    for sym in symbols:
        try:
            out["symbols"][sym] = _per_day_for_symbol(ctx, sym, start_d, end_d, get_bars, s)
        except Exception as exc:
            log.warning("per-day recompute failed for %s: %s", sym, exc)
            out["symbols"][sym] = {"_error": str(exc), "days": []}
    return out


def _per_day_for_symbol(
    ctx: _PerDayContext,
    symbol: str,
    start_d: date,
    end_d: date,
    get_bars: Any,
    session: Session,
) -> dict[str, Any]:
    # Pull a generous warmup so the earliest in-range day has enough history
    # for the 200-day trend term. Same shape `backtest debug` uses.
    warmup = max(250, ctx.required) + 30
    from datetime import timedelta as _td
    bars = get_bars(
        symbol,
        start_d - _td(days=warmup * 2),
        end_d + _td(days=10),
        session=session,
    )
    if bars is None or bars.empty:
        return {"_error": "no bars in local store", "days": []}
    bars = bars.sort_index()
    bars.attrs["symbol"] = symbol

    trading_days = [d for d in sorted({ts.date() for ts in bars.index}) if start_d <= d <= end_d]
    days_out: list[dict[str, Any]] = []
    for day in trading_days:
        view = bars[bars.index.date <= day]
        last_close = float(view["close"].iloc[-1]) if len(view) else None
        if len(view) < ctx.required:
            days_out.append({
                "date": _iso(day),
                "close": _round_or_none(last_close, 4),
                "score": None,
                "phase": "warmup",
            })
            continue
        view_tail = view.tail(ctx.required + 5)
        view_tail.attrs["symbol"] = symbol
        try:
            sc = ctx.strategy.reversal_score(view_tail, day)
        except Exception as exc:
            days_out.append({
                "date": _iso(day),
                "close": _round_or_none(last_close, 4),
                "score": None,
                "phase": "error",
                "error": str(exc),
            })
            continue
        if sc is None:
            days_out.append({
                "date": _iso(day),
                "close": _round_or_none(last_close, 4),
                "score": None,
                "phase": "abstained",
            })
            continue

        b = sc.breakdown
        meta = b.get("_meta", {})
        score = float(sc.score)
        row = {
            "date": _iso(day),
            "close": _round_or_none(last_close, 4),
            "score": round(score, 6),
            "D": meta.get("D"),
            "C": meta.get("C"),
            "reversal_trigger": b.get("reversal_trigger", {}).get("score"),
            "pivot_proximity":  b.get("pivot_proximity",  {}).get("score"),
            "sector_rs":        b.get("sector_rs",        {}).get("score"),
            "trend_location":   b.get("trend_location",   {}).get("score"),
            "volume_confirm":   b.get("volume_confirm",   {}).get("multiplier"),
            "above_entry": bool(score >= ctx.entry_threshold),
            "below_exit":  bool(score <= -ctx.exit_threshold),
        }
        days_out.append(row)
    return {"days": days_out}


# ---------------------------------------------------------------------------
# Summary statistics
# ---------------------------------------------------------------------------
def _summary_stats(trades: list[dict[str, Any]]) -> dict[str, Any]:
    """Derive the table a reviewer reads first: counts, win rate, R-multiple
    distribution, exit-reason mix, and per-input contribution averages
    split by winners vs losers."""
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
    }

    # Per-input contribution averages — only meaningful for strategies that
    # produced a score_breakdown in entry_metadata.
    summary["score_inputs"] = _score_input_breakdown(winners, losers)

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
        "score": (t.get("entry_metadata") or {}).get("reversal_score"),
    }


def _score_input_breakdown(
    winners: list[dict[str, Any]], losers: list[dict[str, Any]]
) -> dict[str, Any]:
    """Average each named score-input across winners vs losers — answers
    "which input was the strongest signal on the trades that worked vs the
    trades that didn't?". Only inputs that appeared in entry_metadata are
    included."""
    def _avg(trades: list[dict[str, Any]], key: str) -> tuple[float | None, int]:
        vals = []
        for t in trades:
            meta = t.get("entry_metadata") or {}
            v = meta.get(key)
            # Some inputs live one level deeper inside the score_breakdown.
            if v is None:
                sb = meta.get("score_breakdown") or {}
                node = sb.get(key)
                if isinstance(node, dict):
                    v = node.get("score") if "score" in node else node.get("multiplier")
            if isinstance(v, (int, float)):
                vals.append(float(v))
        if not vals:
            return None, 0
        return round(sum(vals) / len(vals), 4), len(vals)

    inputs = [
        "reversal_score", "D", "C",
        "reversal_trigger", "pivot_proximity", "sector_rs",
        "trend_location", "volume_confirm",
    ]
    out: dict[str, Any] = {}
    for k in inputs:
        wa, wn = _avg(winners, k)
        la, ln = _avg(losers, k)
        if wn == 0 and ln == 0:
            continue
        out[k] = {
            "winners_mean": wa, "winners_n": wn,
            "losers_mean":  la, "losers_n":  ln,
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


def _parse_date(v: Any) -> date:
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v))


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


def _safe_int(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
