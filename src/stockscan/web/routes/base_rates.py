"""Base rates page (USER_STORIES Story 4)."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from stockscan.analyzer import compute_base_rates
from stockscan.strategies import STRATEGY_REGISTRY, discover_strategies
from stockscan.web.deps import get_session, render

router = APIRouter()


@router.get("/signals/{signal_id}/base-rates")
def base_rates_for_signal(
    signal_id: int,
    request: Request,
    s: Session = Depends(get_session),
):
    """Historical base-rates report for the signal's strategy/symbol pair,
    computed on the fly with the strategy file's knobs. An unknown
    signal renders the empty-state page; a compute failure surfaces as an
    error message instead of a report."""
    discover_strategies()
    sig = s.execute(
        text(
            """
            SELECT signal_id, strategy_name, strategy_version, symbol, as_of_date
            FROM signals WHERE signal_id = :sid
            """
        ),
        {"sid": signal_id},
    ).first()
    if sig is None:
        return render(request, "base_rates/show.html", signal=None, report=None)

    cls = STRATEGY_REGISTRY.get(sig.strategy_name)
    as_of: date = sig.as_of_date

    try:
        report = compute_base_rates(cls, sig.symbol, as_of)
    except Exception as exc:  # noqa: BLE001
        report = None
        error = str(exc)
    else:
        error = None

    return render(
        request,
        "base_rates/show.html",
        signal=sig,
        report=report,
        error=error,
    )
