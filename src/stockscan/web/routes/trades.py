"""Trades pages — list, detail with notes thread (USER_STORIES Stories 5 + 6)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from stockscan.notes import (
    create_note,
    list_notes_for_trade,
    search_notes,
    update_note,
)
from stockscan.positions import (
    get_trade,
    list_closed_paper_trades,
    list_closed_trades,
    list_open_paper_trades,
    list_open_trades,
)
from stockscan.web.deps import flash_redirect, get_session, render

router = APIRouter(prefix="/trades")


@router.get("")
def trades_list(
    request: Request,
    strategy: str | None = Query(None),
    s: Session = Depends(get_session),
):
    """Open + closed trades, real and paper, on one page. ``?strategy=``
    filters the closed tables; open positions always show in full."""
    open_trades = list_open_trades(session=s)
    closed_trades = list_closed_trades(strategy=strategy, session=s)
    open_paper = list_open_paper_trades(session=s)
    closed_paper = list_closed_paper_trades(strategy=strategy, session=s)
    return render(
        request,
        "trades/list.html",
        open_trades=open_trades,
        closed_trades=closed_trades,
        open_paper_trades=open_paper,
        closed_paper_trades=closed_paper,
        active_strategy=strategy,
    )


@router.get("/search")
def trades_search(
    request: Request,
    q: str = Query(..., min_length=2),
    s: Session = Depends(get_session),
):
    """Notes full-text search."""
    results = search_notes(q, session=s)
    return render(
        request,
        "trades/search.html",
        query=q,
        results=results,
    )


@router.get("/{trade_id}")
def trade_detail(
    trade_id: int,
    request: Request,
    s: Session = Depends(get_session),
):
    """Single trade view: the trade itself, its notes thread, and its tax
    lots. An unknown trade_id renders the empty-state page."""
    trade = get_trade(trade_id, session=s)
    if trade is None:
        return render(request, "trades/detail.html", trade=None, notes=[], lots=[])
    notes = list_notes_for_trade(trade_id, session=s)
    # Lots view
    lot_rows = s.execute(
        text(
            """
            SELECT lot_id, qty_original, qty_remaining, cost_basis, acquired_at, closed_at
            FROM tax_lots WHERE trade_id = :tid ORDER BY acquired_at
            """
        ),
        {"tid": trade_id},
    ).all()
    return render(
        request,
        "trades/detail.html",
        trade=trade,
        notes=notes,
        lots=lot_rows,
    )


@router.post("/{trade_id}/notes")
def trade_add_note(
    trade_id: int,
    request: Request,
    body: str = Form(..., min_length=1),
    note_type: str = Form("free"),
    s: Session = Depends(get_session),
):
    """Append a note to a trade's thread, then redirect back to the detail
    page — save failures surface as an error toast instead of a 500."""
    try:
        create_note(trade_id, body=body, note_type=note_type, session=s)  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001 - surface DB / validation errors as a toast
        return flash_redirect(
            f"/trades/{trade_id}", "error", f"Couldn't save note: {exc}"
        )
    return flash_redirect(f"/trades/{trade_id}", "success", "Note saved")


@router.post("/{trade_id}/notes/{note_id}/edit")
def trade_edit_note(
    trade_id: int,
    note_id: int,
    request: Request,
    body: str = Form(..., min_length=1),
    s: Session = Depends(get_session),
):
    """Update an existing note's body, then redirect back to the trade detail
    page — failures surface as an error toast instead of a 500."""
    try:
        update_note(note_id, body=body, session=s)
    except Exception as exc:  # noqa: BLE001 - surface as a toast
        return flash_redirect(
            f"/trades/{trade_id}", "error", f"Couldn't update note: {exc}"
        )
    return flash_redirect(f"/trades/{trade_id}", "success", "Note updated")
