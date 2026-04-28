"""Dashboard route — top-level overview."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from stockscan.db import healthcheck
from stockscan.positions import list_open_trades
from stockscan.strategies import STRATEGY_REGISTRY, discover_strategies
from stockscan.watchlist import watchlist_symbols
from stockscan.web.deps import get_session, render

router = APIRouter()


@router.get("/")
async def dashboard(request: Request, s: Session = Depends(get_session)):
    discover_strategies()

    # Latest equity (or fallback)
    eq_row = s.execute(
        text(
            "SELECT total_equity, cash, positions_value, high_water_mark, as_of_date "
            "FROM equity_history ORDER BY as_of_date DESC LIMIT 1"
        )
    ).first()

    # Latest signals across all strategies
    sig_rows = s.execute(
        text(
            """
            SELECT signal_id, strategy_name, symbol, side, score, status,
                   suggested_entry, suggested_stop, rejected_reason, as_of_date
            FROM signals
            WHERE as_of_date >= :d
            ORDER BY as_of_date DESC, score DESC NULLS LAST
            LIMIT 25
            """
        ),
        {"d": date.today().replace(day=1)},
    ).all()

    open_trades = list_open_trades(session=s)
    health = healthcheck()
    watching = watchlist_symbols(session=s)

    return render(
        request,
        "dashboard.html",
        equity=eq_row,
        signals=sig_rows,
        open_trades=open_trades,
        health=health,
        strategies=STRATEGY_REGISTRY.all(),
        watching=watching,
    )
