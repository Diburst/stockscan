"""Paper-trade CRUD + mark-to-market + auto-close logic.

Paper trades are simulated positions opened directly from signals in the
web UI. They store full entry/exit snapshots (indicators, regime, params)
so every decision is auditable after the fact.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from stockscan.db import session_scope

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PaperTrade:
    paper_trade_id: int
    signal_id: int
    strategy_name: str
    strategy_version: str
    symbol: str
    side: str
    entry_price: Decimal
    stop_price: Decimal
    target_price: Decimal | None
    qty: int
    opened_at: datetime
    entry_signal_metadata: dict[str, Any] | None
    entry_tech_score: dict[str, Any] | None
    entry_regime: dict[str, Any] | None
    entry_strategy_params: dict[str, Any] | None
    current_price: Decimal | None
    unrealised_pnl: Decimal | None
    unrealised_pnl_pct: Decimal | None
    mfe: Decimal | None
    mae: Decimal | None
    last_mark_at: datetime | None
    status: str
    closed_at: datetime | None
    exit_price: Decimal | None
    exit_reason: str | None
    realized_pnl: Decimal | None
    realized_pnl_pct: Decimal | None
    holding_days: int | None
    exit_signal_metadata: dict[str, Any] | None
    exit_tech_score: dict[str, Any] | None
    exit_regime: dict[str, Any] | None
    exit_strategy_params: dict[str, Any] | None
    auto_close_rules: dict[str, Any] | None


_SELECT_COLS = """
    paper_trade_id, signal_id, strategy_name, strategy_version,
    symbol, side, entry_price, stop_price, target_price, qty, opened_at,
    entry_signal_metadata, entry_tech_score, entry_regime, entry_strategy_params,
    current_price, unrealised_pnl, unrealised_pnl_pct,
    max_favorable_excursion, max_adverse_excursion, last_mark_at,
    status, closed_at, exit_price, exit_reason,
    realized_pnl, realized_pnl_pct, holding_days,
    exit_signal_metadata, exit_tech_score, exit_regime, exit_strategy_params,
    auto_close_rules
"""


def _dec(val: Any) -> Decimal | None:
    if val is None:
        return None
    return Decimal(str(val))


def _row_to_paper_trade(r: Any) -> PaperTrade:
    return PaperTrade(
        paper_trade_id=int(r.paper_trade_id),
        signal_id=int(r.signal_id),
        strategy_name=r.strategy_name,
        strategy_version=r.strategy_version,
        symbol=r.symbol,
        side=r.side,
        entry_price=Decimal(str(r.entry_price)),
        stop_price=Decimal(str(r.stop_price)),
        target_price=_dec(r.target_price),
        qty=int(r.qty),
        opened_at=r.opened_at,
        entry_signal_metadata=r.entry_signal_metadata,
        entry_tech_score=r.entry_tech_score,
        entry_regime=r.entry_regime,
        entry_strategy_params=r.entry_strategy_params,
        current_price=_dec(r.current_price),
        unrealised_pnl=_dec(r.unrealised_pnl),
        unrealised_pnl_pct=_dec(r.unrealised_pnl_pct),
        mfe=_dec(r.max_favorable_excursion),
        mae=_dec(r.max_adverse_excursion),
        last_mark_at=r.last_mark_at,
        status=r.status,
        closed_at=r.closed_at,
        exit_price=_dec(r.exit_price),
        exit_reason=r.exit_reason,
        realized_pnl=_dec(r.realized_pnl),
        realized_pnl_pct=_dec(r.realized_pnl_pct),
        holding_days=int(r.holding_days) if r.holding_days is not None else None,
        exit_signal_metadata=r.exit_signal_metadata,
        exit_tech_score=r.exit_tech_score,
        exit_regime=r.exit_regime,
        exit_strategy_params=r.exit_strategy_params,
        auto_close_rules=r.auto_close_rules,
    )


# ---------- Create ----------


def open_paper_trade(
    *,
    signal_id: int,
    strategy_name: str,
    strategy_version: str,
    symbol: str,
    side: str,
    entry_price: Decimal,
    stop_price: Decimal,
    target_price: Decimal | None,
    qty: int,
    entry_signal_metadata: dict[str, Any] | None = None,
    entry_tech_score: dict[str, Any] | None = None,
    entry_regime: dict[str, Any] | None = None,
    entry_strategy_params: dict[str, Any] | None = None,
    auto_close_rules: dict[str, Any] | None = None,
    session: Session | None = None,
) -> int:
    """Open a new paper trade. Returns the paper_trade_id."""
    import json

    sql = text(
        """
        INSERT INTO paper_trades (
            signal_id, strategy_name, strategy_version, symbol, side,
            entry_price, stop_price, target_price, qty,
            entry_signal_metadata, entry_tech_score, entry_regime,
            entry_strategy_params, auto_close_rules,
            current_price, unrealised_pnl, unrealised_pnl_pct
        ) VALUES (
            :signal_id, :strategy_name, :strategy_version, :symbol, :side,
            :entry_price, :stop_price, :target_price, :qty,
            :entry_signal_metadata, :entry_tech_score, :entry_regime,
            :entry_strategy_params, :auto_close_rules,
            :entry_price, 0, 0
        ) RETURNING paper_trade_id
        """
    )
    params = {
        "signal_id": signal_id,
        "strategy_name": strategy_name,
        "strategy_version": strategy_version,
        "symbol": symbol,
        "side": side,
        "entry_price": entry_price,
        "stop_price": stop_price,
        "target_price": target_price,
        "qty": qty,
        "entry_signal_metadata": json.dumps(entry_signal_metadata) if entry_signal_metadata else None,
        "entry_tech_score": json.dumps(entry_tech_score) if entry_tech_score else None,
        "entry_regime": json.dumps(entry_regime) if entry_regime else None,
        "entry_strategy_params": json.dumps(entry_strategy_params) if entry_strategy_params else None,
        "auto_close_rules": json.dumps(auto_close_rules) if auto_close_rules else None,
    }

    def _run(s: Session) -> int:
        row = s.execute(sql, params).first()
        assert row is not None
        return int(row.paper_trade_id)

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)


# ---------- Read ----------


def get_paper_trade(paper_trade_id: int, *, session: Session | None = None) -> PaperTrade | None:
    sql = text(f"SELECT {_SELECT_COLS} FROM paper_trades WHERE paper_trade_id = :pid")

    def _run(s: Session) -> PaperTrade | None:
        row = s.execute(sql, {"pid": paper_trade_id}).first()
        return _row_to_paper_trade(row) if row else None

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)


def list_open_paper_trades(*, session: Session | None = None) -> list[PaperTrade]:
    sql = text(
        f"SELECT {_SELECT_COLS} FROM paper_trades WHERE status = 'open' ORDER BY opened_at DESC"
    )
    if session is not None:
        return [_row_to_paper_trade(r) for r in session.execute(sql)]
    with session_scope() as s:
        return [_row_to_paper_trade(r) for r in s.execute(sql)]


def list_closed_paper_trades(
    *,
    limit: int = 100,
    strategy: str | None = None,
    session: Session | None = None,
) -> list[PaperTrade]:
    if strategy:
        sql = text(
            f"""SELECT {_SELECT_COLS} FROM paper_trades
            WHERE status = 'closed' AND strategy_name = :strat
            ORDER BY closed_at DESC LIMIT :lim"""
        )
        params: dict[str, Any] = {"strat": strategy, "lim": limit}
    else:
        sql = text(
            f"""SELECT {_SELECT_COLS} FROM paper_trades
            WHERE status = 'closed'
            ORDER BY closed_at DESC LIMIT :lim"""
        )
        params = {"lim": limit}

    if session is not None:
        return [_row_to_paper_trade(r) for r in session.execute(sql, params)]
    with session_scope() as s:
        return [_row_to_paper_trade(r) for r in s.execute(sql, params)]


# ---------- Close ----------


def close_paper_trade(
    paper_trade_id: int,
    *,
    exit_price: Decimal,
    exit_reason: str,
    exit_signal_metadata: dict[str, Any] | None = None,
    exit_tech_score: dict[str, Any] | None = None,
    exit_regime: dict[str, Any] | None = None,
    exit_strategy_params: dict[str, Any] | None = None,
    session: Session | None = None,
) -> None:
    """Close an open paper trade with the given exit price and reason."""
    import json

    sql = text(
        """
        UPDATE paper_trades SET
            status = 'closed',
            closed_at = NOW(),
            exit_price = :exit_price,
            exit_reason = :exit_reason,
            realized_pnl = CASE
                WHEN side = 'long' THEN (:exit_price - entry_price) * qty
                ELSE (entry_price - :exit_price) * qty
            END,
            realized_pnl_pct = CASE
                WHEN side = 'long' THEN (:exit_price - entry_price) / entry_price
                ELSE (entry_price - :exit_price) / entry_price
            END,
            holding_days = EXTRACT(DAY FROM NOW() - opened_at)::integer,
            exit_signal_metadata = :exit_signal_metadata,
            exit_tech_score = :exit_tech_score,
            exit_regime = :exit_regime,
            exit_strategy_params = :exit_strategy_params
        WHERE paper_trade_id = :pid AND status = 'open'
        """
    )
    params = {
        "pid": paper_trade_id,
        "exit_price": exit_price,
        "exit_reason": exit_reason,
        "exit_signal_metadata": json.dumps(exit_signal_metadata) if exit_signal_metadata else None,
        "exit_tech_score": json.dumps(exit_tech_score) if exit_tech_score else None,
        "exit_regime": json.dumps(exit_regime) if exit_regime else None,
        "exit_strategy_params": json.dumps(exit_strategy_params) if exit_strategy_params else None,
    }

    if session is not None:
        session.execute(sql, params)
    else:
        with session_scope() as s:
            s.execute(sql, params)


# ---------- Mark-to-market ----------


def mark_to_market(*, session: Session | None = None) -> int:
    """Update unrealised P/L and excursions for all open paper trades
    using the latest bar close price.

    Returns the number of trades updated.
    """
    sql = text(
        """
        WITH latest_close AS (
            SELECT DISTINCT ON (symbol)
                symbol, close AS price, bar_ts
            FROM bars
            WHERE interval = '1d'
            ORDER BY symbol, bar_ts DESC
        )
        UPDATE paper_trades pt SET
            current_price = lc.price,
            unrealised_pnl = CASE
                WHEN pt.side = 'long' THEN (lc.price - pt.entry_price) * pt.qty
                ELSE (pt.entry_price - lc.price) * pt.qty
            END,
            unrealised_pnl_pct = CASE
                WHEN pt.side = 'long' THEN (lc.price - pt.entry_price) / pt.entry_price
                ELSE (pt.entry_price - lc.price) / pt.entry_price
            END,
            max_favorable_excursion = GREATEST(
                COALESCE(pt.max_favorable_excursion, 0),
                CASE
                    WHEN pt.side = 'long' THEN (lc.price - pt.entry_price) / pt.entry_price
                    ELSE (pt.entry_price - lc.price) / pt.entry_price
                END
            ),
            max_adverse_excursion = LEAST(
                COALESCE(pt.max_adverse_excursion, 0),
                CASE
                    WHEN pt.side = 'long' THEN (lc.price - pt.entry_price) / pt.entry_price
                    ELSE (pt.entry_price - lc.price) / pt.entry_price
                END
            ),
            last_mark_at = NOW()
        FROM latest_close lc
        WHERE pt.symbol = lc.symbol AND pt.status = 'open'
        """
    )

    def _run(s: Session) -> int:
        result = s.execute(sql)
        return result.rowcount  # type: ignore[return-value]

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)


def check_auto_close(*, session: Session | None = None) -> list[int]:
    """Check open paper trades against their auto_close_rules and close
    any that meet exit criteria.

    Checks:
      - stop_hit: current_price <= stop_price (long) or >= stop_price (short)
      - target_hit: current_price >= target_price (long) or <= target_price (short)
      - time_stop: holding days >= auto_close_rules.time_stop_days

    Returns list of paper_trade_ids that were auto-closed.
    """
    open_trades = list_open_paper_trades(session=session)
    closed_ids: list[int] = []

    for pt in open_trades:
        if pt.current_price is None:
            continue

        exit_reason: str | None = None
        exit_price = pt.current_price

        # Stop loss check
        if pt.side == "long" and pt.current_price <= pt.stop_price:
            exit_reason = "stop_hit"
            exit_price = pt.stop_price
        elif pt.side == "short" and pt.current_price >= pt.stop_price:
            exit_reason = "stop_hit"
            exit_price = pt.stop_price

        # Target check
        if exit_reason is None and pt.target_price is not None:
            if pt.side == "long" and pt.current_price >= pt.target_price:
                exit_reason = "target_hit"
                exit_price = pt.target_price
            elif pt.side == "short" and pt.current_price <= pt.target_price:
                exit_reason = "target_hit"
                exit_price = pt.target_price

        # Time stop check (from auto_close_rules)
        if exit_reason is None and pt.auto_close_rules:
            time_stop_days = pt.auto_close_rules.get("time_stop_days")
            if time_stop_days is not None:
                elapsed = (datetime.now(timezone.utc) - pt.opened_at).days
                if elapsed >= time_stop_days:
                    exit_reason = "time_stop"

            # Strategy-specific exit rules from auto_close_rules
            chandelier_stop = pt.auto_close_rules.get("chandelier_stop")
            if exit_reason is None and chandelier_stop is not None:
                if pt.side == "long" and float(pt.current_price) <= chandelier_stop:
                    exit_reason = "chandelier_stop"
                elif pt.side == "short" and float(pt.current_price) >= chandelier_stop:
                    exit_reason = "chandelier_stop"

            exit_below_n_day_low = pt.auto_close_rules.get("exit_below_n_day_low")
            if exit_reason is None and exit_below_n_day_low is not None:
                if pt.side == "long" and float(pt.current_price) <= exit_below_n_day_low:
                    exit_reason = "exit_below_n_day_low"

        if exit_reason is not None:
            log.info(
                "auto-closing paper trade #%d (%s) — %s at $%.2f",
                pt.paper_trade_id, pt.symbol, exit_reason, float(exit_price),
            )
            close_paper_trade(
                pt.paper_trade_id,
                exit_price=exit_price,
                exit_reason=exit_reason,
                session=session,
            )
            closed_ids.append(pt.paper_trade_id)

    return closed_ids
