"""Options tab — the proposed short-premium book, computed on demand.

  GET /options          - ranked proposal cards for the watchlist.

Computes live via the proposal engine. Persisted runs (the nightly save and
the settle step) only feed the trigger-class base rates on the cards.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from stockscan.proposals import generate_book
from stockscan.proposals.store import BASE_RATE_MIN_N, trigger_base_rates
from stockscan.web.deps import get_session, render

router = APIRouter(prefix="/options")
log = logging.getLogger(__name__)


@router.get("")
def options_page(
    request: Request,
    list: str | None = Query(None),
    n: int = Query(30, ge=1, le=100),
    s: Session = Depends(get_session),
):
    """Render the proposed options book (ranked cards) for the watchlist."""
    list_id = int(list) if list and list.isdigit() else None
    run = generate_book(n=n, list_id=list_id, session=s)
    try:
        rates = trigger_base_rates(session=s)
    except Exception as exc:
        log.warning("options: trigger_base_rates() failed: %s", exc)
        rates = {}
    gate_open = run.regime.trend_gate_open if run.regime is not None else None
    base_rates = {
        p.symbol: rates.get((p.side, p.trend_bucket, gate_open)) for p in run.book
    }
    return render(
        request,
        "options/list.html",
        book=run.book,
        regime=run.regime,
        as_of=run.as_of,
        candidates=run.candidates,
        book_mult=run.book_mult,
        macro_events=run.macro_events,
        equity=run.equity,
        base_rates=base_rates,
        base_rate_min_n=BASE_RATE_MIN_N,
        n=n,
    )
