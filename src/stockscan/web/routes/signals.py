"""Signals page — passing + rejected with badges (USER_STORIES Story 1)."""

from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from stockscan.strategies import STRATEGY_REGISTRY, discover_strategies
from stockscan.web.deps import get_session, render

router = APIRouter(prefix="/signals")


@router.get("")
async def signals_list(
    request: Request,
    strategy: str | None = Query(None),
    days: int = Query(7, ge=1, le=90),
    show_rejected: bool = Query(True),
    s: Session = Depends(get_session),
):
    discover_strategies()
    cutoff = date.today() - timedelta(days=days)

    where = ["s.as_of_date >= :d"]
    params: dict[str, object] = {"d": cutoff}
    if strategy:
        where.append("s.strategy_name = :strat")
        params["strat"] = strategy
    if not show_rejected:
        where.append("s.status = 'new'")

    sql = text(
        f"""
        SELECT s.signal_id, s.run_id, s.strategy_name, s.symbol, s.side,
               s.score, s.status, s.as_of_date,
               s.suggested_entry, s.suggested_stop, s.suggested_qty,
               s.rejected_reason, s.metadata,
               t.score AS tech_score
        FROM signals s
        LEFT JOIN technical_scores t
          ON t.symbol = s.symbol
         AND t.as_of_date = s.as_of_date
         AND t.strategy_name = s.strategy_name
        WHERE {' AND '.join(where)}
        ORDER BY s.as_of_date DESC, s.status ASC, s.score DESC NULLS LAST
        LIMIT 500
        """
    )
    rows = s.execute(sql, params).all()
    passing = [r for r in rows if r.status == "new"]
    rejected = [r for r in rows if r.status == "rejected"]

    return render(
        request,
        "signals/list.html",
        passing=passing,
        rejected=rejected,
        strategies=STRATEGY_REGISTRY.all(),
        active_strategy=strategy,
        show_rejected=show_rejected,
        days=days,
    )


@router.get("/{signal_id}")
async def signal_detail(
    signal_id: int,
    request: Request,
    s: Session = Depends(get_session),
):
    sql = text(
        """
        SELECT signal_id, strategy_name, strategy_version, symbol, side,
               score, status, as_of_date, suggested_entry, suggested_stop,
               suggested_target, suggested_qty, rejected_reason, metadata
        FROM signals WHERE signal_id = :sid
        """
    )
    sig = s.execute(sql, {"sid": signal_id}).first()
    if sig is None:
        return render(request, "signals/detail.html", signal=None)
    return render(request, "signals/detail.html", signal=sig)
