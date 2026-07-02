"""Delta-hedge persistence — CRUD, tick-state marks, adjustments, settlement.

Direct-SQL in the same house style as ``positions/paper_store.py``. The daemon
owns all the math (see ``hedge.accounting`` / ``hedge.policy``); this module just
reads and writes rows, keeping the ``held_shares`` / ``avg_cost`` /
``realized_hedge_pnl`` cache on ``hedge_positions`` consistent with the
``hedge_adjustments`` ledger by updating both in one transaction.

Three tables (migration 0024):
  * ``hedge_positions``   — one row per fake option position being hedged.
  * ``hedge_adjustments`` — the append-only ledger of every stock fill.
  * ``hedge_heartbeat``   — single-row daemon liveness for the UI.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from stockscan.db import session_scope

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class HedgePosition:
    hedge_position_id: int
    symbol: str
    option_kind: str
    option_side: str
    strike: Decimal
    contracts: int
    multiplier: int
    expiry: datetime
    premium: Decimal
    iv_pct: Decimal | None
    rate_pct: Decimal | None
    band_policy: dict[str, Any] | None
    status: str
    held_shares: int
    avg_cost: Decimal
    realized_hedge_pnl: Decimal
    last_spot: Decimal | None
    last_delta: Decimal | None
    last_target_shares: int | None
    last_hedge_spot: Decimal | None
    last_tick_at: datetime | None
    iv_refreshed_on: Any | None
    created_at: datetime
    closed_at: datetime | None
    close_reason: str | None
    settlement_spot: Decimal | None
    realized_pnl: Decimal | None
    notes: str | None


@dataclass(frozen=True, slots=True)
class HedgeAdjustment:
    adjustment_id: int
    hedge_position_id: int
    ts: datetime
    side: str
    qty: int
    price: Decimal
    spot: Decimal | None
    option_delta: Decimal | None
    target_shares: int | None
    held_before: int | None
    held_after: int | None
    reason: str | None
    realized_pnl_delta: Decimal | None


_POS_COLS = """
    hedge_position_id, symbol, option_kind, option_side, strike, contracts,
    multiplier, expiry, premium, iv_pct, rate_pct, band_policy, status,
    held_shares, avg_cost, realized_hedge_pnl, last_spot, last_delta,
    last_target_shares, last_hedge_spot, last_tick_at, iv_refreshed_on,
    created_at, closed_at, close_reason, settlement_spot, realized_pnl, notes
"""

_ADJ_COLS = """
    adjustment_id, hedge_position_id, ts, side, qty, price, spot,
    option_delta, target_shares, held_before, held_after, reason,
    realized_pnl_delta
"""


def _dec(val: Any) -> Decimal | None:
    return None if val is None else Decimal(str(val))


def _row_to_position(r: Any) -> HedgePosition:
    return HedgePosition(
        hedge_position_id=int(r.hedge_position_id),
        symbol=r.symbol,
        option_kind=r.option_kind,
        option_side=r.option_side,
        strike=Decimal(str(r.strike)),
        contracts=int(r.contracts),
        multiplier=int(r.multiplier),
        expiry=r.expiry,
        premium=Decimal(str(r.premium)),
        iv_pct=_dec(r.iv_pct),
        rate_pct=_dec(r.rate_pct),
        band_policy=r.band_policy,
        status=r.status,
        held_shares=int(r.held_shares),
        avg_cost=Decimal(str(r.avg_cost)),
        realized_hedge_pnl=Decimal(str(r.realized_hedge_pnl)),
        last_spot=_dec(r.last_spot),
        last_delta=_dec(r.last_delta),
        last_target_shares=int(r.last_target_shares) if r.last_target_shares is not None else None,
        last_hedge_spot=_dec(r.last_hedge_spot),
        last_tick_at=r.last_tick_at,
        iv_refreshed_on=r.iv_refreshed_on,
        created_at=r.created_at,
        closed_at=r.closed_at,
        close_reason=r.close_reason,
        settlement_spot=_dec(r.settlement_spot),
        realized_pnl=_dec(r.realized_pnl),
        notes=r.notes,
    )


def _row_to_adjustment(r: Any) -> HedgeAdjustment:
    return HedgeAdjustment(
        adjustment_id=int(r.adjustment_id),
        hedge_position_id=int(r.hedge_position_id),
        ts=r.ts,
        side=r.side,
        qty=int(r.qty),
        price=Decimal(str(r.price)),
        spot=_dec(r.spot),
        option_delta=_dec(r.option_delta),
        target_shares=int(r.target_shares) if r.target_shares is not None else None,
        held_before=int(r.held_before) if r.held_before is not None else None,
        held_after=int(r.held_after) if r.held_after is not None else None,
        reason=r.reason,
        realized_pnl_delta=_dec(r.realized_pnl_delta),
    )


# ---------- Create ----------


def create_hedge_position(
    *,
    symbol: str,
    option_kind: str,
    option_side: str,
    strike: Decimal,
    contracts: int,
    expiry: datetime,
    premium: Decimal,
    iv_pct: float | None,
    rate_pct: float | None,
    band_policy: dict[str, Any],
    multiplier: int = 100,
    notes: str | None = None,
    session: Session | None = None,
) -> int:
    """Insert a new (active) hedge position. Returns its id."""
    sql = text(
        """
        INSERT INTO hedge_positions (
            symbol, option_kind, option_side, strike, contracts, multiplier,
            expiry, premium, iv_pct, rate_pct, band_policy, status, notes
        ) VALUES (
            :symbol, :kind, :side, :strike, :contracts, :multiplier,
            :expiry, :premium, :iv_pct, :rate_pct, :band_policy, 'active', :notes
        ) RETURNING hedge_position_id
        """
    )
    params = {
        "symbol": symbol,
        "kind": option_kind,
        "side": option_side,
        "strike": strike,
        "contracts": contracts,
        "multiplier": multiplier,
        "expiry": expiry,
        "premium": premium,
        "iv_pct": iv_pct,
        "rate_pct": rate_pct,
        "band_policy": json.dumps(band_policy),
        "notes": notes,
    }

    def _run(s: Session) -> int:
        row = s.execute(sql, params).first()
        assert row is not None
        return int(row.hedge_position_id)

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)


# ---------- Read ----------


def get_hedge_position(hedge_position_id: int, *, session: Session | None = None) -> HedgePosition | None:
    sql = text(f"SELECT {_POS_COLS} FROM hedge_positions WHERE hedge_position_id = :pid")

    def _run(s: Session) -> HedgePosition | None:
        row = s.execute(sql, {"pid": hedge_position_id}).first()
        return _row_to_position(row) if row else None

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)


def list_hedge_positions(
    *, status: str | None = None, session: Session | None = None
) -> list[HedgePosition]:
    if status:
        sql = text(f"SELECT {_POS_COLS} FROM hedge_positions WHERE status = :st ORDER BY created_at DESC")
        params: dict[str, Any] = {"st": status}
    else:
        sql = text(f"SELECT {_POS_COLS} FROM hedge_positions ORDER BY created_at DESC")
        params = {}

    def _run(s: Session) -> list[HedgePosition]:
        return [_row_to_position(r) for r in s.execute(sql, params)]

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)


def list_active_hedge_positions(*, session: Session | None = None) -> list[HedgePosition]:
    """Positions the daemon should be hedging right now (status = 'active')."""
    return list_hedge_positions(status="active", session=session)


def list_adjustments(
    hedge_position_id: int, *, limit: int = 500, session: Session | None = None
) -> list[HedgeAdjustment]:
    sql = text(
        f"""SELECT {_ADJ_COLS} FROM hedge_adjustments
        WHERE hedge_position_id = :pid ORDER BY ts DESC, adjustment_id DESC LIMIT :lim"""
    )
    params = {"pid": hedge_position_id, "lim": limit}

    def _run(s: Session) -> list[HedgeAdjustment]:
        return [_row_to_adjustment(r) for r in s.execute(sql, params)]

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)


# ---------- Update: tick state (no trade) ----------


def update_tick_state(
    hedge_position_id: int,
    *,
    last_spot: float,
    last_delta: float,
    last_target_shares: int,
    last_tick_at: datetime,
    session: Session | None = None,
) -> None:
    """Persist the latest mark for a position without trading (throttled writes)."""
    sql = text(
        """
        UPDATE hedge_positions SET
            last_spot = :spot, last_delta = :delta,
            last_target_shares = :target, last_tick_at = :ts
        WHERE hedge_position_id = :pid
        """
    )
    params = {
        "pid": hedge_position_id,
        "spot": last_spot,
        "delta": last_delta,
        "target": last_target_shares,
        "ts": last_tick_at,
    }
    if session is not None:
        session.execute(sql, params)
    else:
        with session_scope() as s:
            s.execute(sql, params)


# ---------- Update: an actual stock fill (ledger + cache in one tx) ----------


def apply_adjustment(
    hedge_position_id: int,
    *,
    fill_qty: int,
    fill_price: float,
    spot: float,
    option_delta: float,
    target_shares: int,
    held_before: int,
    held_after: int,
    new_avg_cost: float,
    new_realized_hedge_pnl: float,
    realized_pnl_delta: float,
    reason: str,
    tick_at: datetime | None = None,
    session: Session | None = None,
) -> int:
    """Record one stock fill and update the position's cached state atomically.

    The caller (daemon) has already run ``accounting.apply_fill`` to compute
    ``held_after`` / ``new_avg_cost`` / ``new_realized_hedge_pnl``; this persists
    the ledger row and the cache together. Returns the adjustment id.
    """
    side = "buy" if fill_qty > 0 else "sell"
    ins = text(
        """
        INSERT INTO hedge_adjustments (
            hedge_position_id, side, qty, price, spot, option_delta,
            target_shares, held_before, held_after, reason, realized_pnl_delta
        ) VALUES (
            :pid, :side, :qty, :price, :spot, :delta,
            :target, :before, :after, :reason, :rpnl
        ) RETURNING adjustment_id
        """
    )
    upd = text(
        """
        UPDATE hedge_positions SET
            held_shares = :after,
            avg_cost = :avg_cost,
            realized_hedge_pnl = :realized,
            last_spot = :spot,
            last_delta = :delta,
            last_target_shares = :target,
            last_hedge_spot = :spot,
            last_tick_at = COALESCE(:ts, NOW())
        WHERE hedge_position_id = :pid
        """
    )
    ins_params = {
        "pid": hedge_position_id,
        "side": side,
        "qty": abs(fill_qty),
        "price": fill_price,
        "spot": spot,
        "delta": option_delta,
        "target": target_shares,
        "before": held_before,
        "after": held_after,
        "reason": reason,
        "rpnl": realized_pnl_delta,
    }
    upd_params = {
        "pid": hedge_position_id,
        "after": held_after,
        "avg_cost": new_avg_cost,
        "realized": new_realized_hedge_pnl,
        "spot": spot,
        "delta": option_delta,
        "target": target_shares,
        "ts": tick_at,
    }

    def _run(s: Session) -> int:
        row = s.execute(ins, ins_params).first()
        assert row is not None
        s.execute(upd, upd_params)
        return int(row.adjustment_id)

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)


# ---------- Status + close ----------


def set_status(hedge_position_id: int, status: str, *, session: Session | None = None) -> None:
    """Set status to 'active' or 'paused' (use :func:`close_hedge_position` to close)."""
    if status not in {"active", "paused"}:
        raise ValueError(f"use close_hedge_position to close; got status={status!r}")
    sql = text("UPDATE hedge_positions SET status = :st WHERE hedge_position_id = :pid AND status <> 'closed'")
    params = {"st": status, "pid": hedge_position_id}
    if session is not None:
        session.execute(sql, params)
    else:
        with session_scope() as s:
            s.execute(sql, params)


def close_hedge_position(
    hedge_position_id: int,
    *,
    close_reason: str,
    settlement_spot: float,
    realized_pnl: float,
    session: Session | None = None,
) -> None:
    """Mark a position closed with its final booked P&L (post-settlement)."""
    sql = text(
        """
        UPDATE hedge_positions SET
            status = 'closed',
            closed_at = NOW(),
            close_reason = :reason,
            settlement_spot = :spot,
            realized_pnl = :realized,
            held_shares = 0
        WHERE hedge_position_id = :pid AND status <> 'closed'
        """
    )
    params = {
        "pid": hedge_position_id,
        "reason": close_reason,
        "spot": settlement_spot,
        "realized": realized_pnl,
    }
    if session is not None:
        session.execute(sql, params)
    else:
        with session_scope() as s:
            s.execute(sql, params)


def refresh_iv(hedge_position_id: int, iv_pct: float, on_date: Any, *, session: Session | None = None) -> None:
    """Update the stored σ (called once per day by the daemon)."""
    sql = text(
        "UPDATE hedge_positions SET iv_pct = :iv, iv_refreshed_on = :d WHERE hedge_position_id = :pid"
    )
    params = {"iv": iv_pct, "d": on_date, "pid": hedge_position_id}
    if session is not None:
        session.execute(sql, params)
    else:
        with session_scope() as s:
            s.execute(sql, params)


# ---------- Heartbeat ----------


def write_heartbeat(
    *,
    pid: int,
    feed_kind: str,
    active_symbols: int,
    status: str = "running",
    note: str | None = None,
    started: bool = False,
    session: Session | None = None,
) -> None:
    """Upsert the single-row daemon heartbeat. ``started=True`` stamps started_at."""
    started_clause = "started_at = NOW()," if started else ""
    sql = text(
        f"""
        INSERT INTO hedge_heartbeat (id, pid, started_at, last_heartbeat_at, feed_kind, active_symbols, status, note)
        VALUES (1, :pid, NOW(), NOW(), :feed, :n, :status, :note)
        ON CONFLICT (id) DO UPDATE SET
            pid = :pid,
            {started_clause}
            last_heartbeat_at = NOW(),
            feed_kind = :feed,
            active_symbols = :n,
            status = :status,
            note = :note
        """
    )
    params = {"pid": pid, "feed": feed_kind, "n": active_symbols, "status": status, "note": note}
    if session is not None:
        session.execute(sql, params)
    else:
        with session_scope() as s:
            s.execute(sql, params)


def get_heartbeat(*, session: Session | None = None) -> dict[str, Any] | None:
    sql = text(
        """SELECT pid, started_at, last_heartbeat_at, feed_kind, active_symbols, status, note
        FROM hedge_heartbeat WHERE id = 1"""
    )

    def _run(s: Session) -> dict[str, Any] | None:
        row = s.execute(sql).first()
        if row is None:
            return None
        return {
            "pid": row.pid,
            "started_at": row.started_at,
            "last_heartbeat_at": row.last_heartbeat_at,
            "feed_kind": row.feed_kind,
            "active_symbols": row.active_symbols,
            "status": row.status,
            "note": row.note,
        }

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)
