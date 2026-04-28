"""Trade lookup helpers used by the Trades pages."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from stockscan.db import session_scope


@dataclass(frozen=True, slots=True)
class Trade:
    trade_id: int
    account_id: int
    symbol: str
    strategy: str
    opened_at: datetime
    closed_at: datetime | None
    status: str
    realized_pnl: Decimal | None
    holding_days: int | None
    mfe: Decimal | None
    mae: Decimal | None


def _row_to_trade(r: Any) -> Trade:
    return Trade(
        trade_id=int(r.trade_id),
        account_id=int(r.account_id),
        symbol=r.symbol,
        strategy=r.strategy,
        opened_at=r.opened_at,
        closed_at=r.closed_at,
        status=r.status,
        realized_pnl=Decimal(str(r.realized_pnl)) if r.realized_pnl is not None else None,
        holding_days=int(r.holding_days) if r.holding_days is not None else None,
        mfe=Decimal(str(r.max_favorable_excursion)) if r.max_favorable_excursion is not None else None,
        mae=Decimal(str(r.max_adverse_excursion)) if r.max_adverse_excursion is not None else None,
    )


def list_open_trades(*, session: Session | None = None) -> list[Trade]:
    sql = text(
        """
        SELECT trade_id, account_id, symbol, strategy, opened_at, closed_at, status,
               realized_pnl, holding_days,
               max_favorable_excursion, max_adverse_excursion
        FROM trades WHERE status = 'open'
        ORDER BY opened_at DESC
        """
    )
    if session is not None:
        return [_row_to_trade(r) for r in session.execute(sql)]
    with session_scope() as s:
        return [_row_to_trade(r) for r in s.execute(sql)]


def list_closed_trades(
    *,
    limit: int = 100,
    strategy: str | None = None,
    session: Session | None = None,
) -> list[Trade]:
    if strategy:
        sql = text(
            """
            SELECT trade_id, account_id, symbol, strategy, opened_at, closed_at, status,
                   realized_pnl, holding_days,
                   max_favorable_excursion, max_adverse_excursion
            FROM trades WHERE status = 'closed' AND strategy = :strat
            ORDER BY closed_at DESC LIMIT :lim
            """
        )
        params = {"strat": strategy, "lim": limit}
    else:
        sql = text(
            """
            SELECT trade_id, account_id, symbol, strategy, opened_at, closed_at, status,
                   realized_pnl, holding_days,
                   max_favorable_excursion, max_adverse_excursion
            FROM trades WHERE status = 'closed'
            ORDER BY closed_at DESC LIMIT :lim
            """
        )
        params = {"lim": limit}

    if session is not None:
        return [_row_to_trade(r) for r in session.execute(sql, params)]
    with session_scope() as s:
        return [_row_to_trade(r) for r in s.execute(sql, params)]


def get_trade(trade_id: int, *, session: Session | None = None) -> Trade | None:
    sql = text(
        """
        SELECT trade_id, account_id, symbol, strategy, opened_at, closed_at, status,
               realized_pnl, holding_days,
               max_favorable_excursion, max_adverse_excursion
        FROM trades WHERE trade_id = :tid
        """
    )

    def _run(s: Session) -> Trade | None:
        row = s.execute(sql, {"tid": trade_id}).first()
        return _row_to_trade(row) if row else None

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)
