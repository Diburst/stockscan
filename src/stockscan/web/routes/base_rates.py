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
async def base_rates_for_signal(
    signal_id: int,
    request: Request,
    s: Session = Depends(get_session),
):
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

    # File defaults — strategy_configs is retired; the strategy file is the
    # source of truth for params. Strategies without a params_model are
    # instantiated with no args.
    cls = STRATEGY_REGISTRY.get(sig.strategy_name)
    params = cls.params_model() if cls.params_model is not None else None
    as_of: date = sig.as_of_date

    try:
        report = compute_base_rates(cls, params, sig.symbol, as_of)
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
