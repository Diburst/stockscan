"""Options tab — the proposed short-premium book, computed on demand.

  GET /options          - ranked, regime-sized proposal cards for the watchlist.

Computes live via the proposal engine (no persistence needed to view). Persisted
runs come from `stockscan options propose --save`.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from stockscan.proposals import generate_book
from stockscan.proposals.engine import SCORE_INPUTS
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
    return render(
        request,
        "options/list.html",
        book=run.book,
        regime=run.regime,
        as_of=run.as_of,
        candidates=run.candidates,
        score_inputs=SCORE_INPUTS,
        n=n,
    )
