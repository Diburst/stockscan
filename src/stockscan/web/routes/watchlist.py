"""Watchlist page — list, add, remove, set/clear target, toggle alert."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Literal

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from datetime import date as _date

from stockscan.data.store import get_bars
from stockscan.technical import compute_technical_score
from stockscan.watchlist.store import (
    add_to_watchlist,
    list_watchlist,
    remove_from_watchlist,
    set_target,
    toggle_alert,
)
from stockscan.web.deps import get_session, render

router = APIRouter(prefix="/watchlist")


@router.get("")
async def watchlist_list(
    request: Request,
    err: str | None = Query(None),
    s: Session = Depends(get_session),
):
    items = list_watchlist(session=s)
    # Compute neutral-mode technical scores for each symbol on the fly. The
    # watchlist is small (~20-50 names), and bars are already in the local DB,
    # so this is fast — sub-100ms typical. No caching layer needed at this scale.
    today = _date.today()
    tech_scores: dict[str, float | None] = {}
    for item in items:
        try:
            bars = get_bars(item.symbol, today.replace(year=today.year - 1), today, session=s)
        except Exception:
            tech_scores[item.symbol] = None
            continue
        if bars is None or bars.empty:
            tech_scores[item.symbol] = None
            continue
        result = compute_technical_score(None, bars, today)
        tech_scores[item.symbol] = result.score if result is not None else None
    return render(
        request,
        "watchlist/list.html",
        items=items,
        tech_scores=tech_scores,
        err=err,
    )


# Inline replacement returned to HTMX requests after a successful add — the
# Dashboard's "+ Watch" button is replaced with this in-place (no page reload).
_WATCHING_SNIPPET = (
    '<span class="text-xs px-2 py-1 rounded bg-ok-100 text-ok-600 '
    'border border-ok-600/30 inline-block">✓ watching</span>'
)
_WATCH_ERROR_SNIPPET = (
    '<span class="text-xs px-2 py-1 rounded bg-bad-100 text-bad-600 '
    'border border-bad-600/30 inline-block">✗ error</span>'
)


def _is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request", "").lower() == "true"


@router.post("/add")
async def watchlist_add(
    request: Request,
    symbol: str = Form(..., min_length=1, max_length=10),
    target_price: str = Form(""),
    target_direction: str = Form(""),
    note: str = Form(""),
    redirect_to: str = Form("/watchlist"),
    s: Session = Depends(get_session),
):
    try:
        tp: Decimal | None = None
        td: Literal["above", "below"] | None = None
        if target_price.strip():
            tp = Decimal(target_price.strip())
        if target_direction.strip():
            if target_direction not in {"above", "below"}:
                raise ValueError("target_direction must be 'above' or 'below'")
            td = target_direction  # type: ignore[assignment]
        add_to_watchlist(
            symbol,
            target_price=tp,
            target_direction=td,
            note=note.strip() or None,
            session=s,
        )
    except (ValueError, InvalidOperation) as exc:
        if _is_htmx(request):
            return HTMLResponse(_WATCH_ERROR_SNIPPET, status_code=400)
        return RedirectResponse(
            url=f"/watchlist?err={str(exc).replace(' ', '+')}", status_code=303
        )

    if _is_htmx(request):
        # Replace the form in-place; no page reload, no scroll jump.
        return HTMLResponse(_WATCHING_SNIPPET)
    return RedirectResponse(url=redirect_to, status_code=303)


@router.post("/{watchlist_id}/delete")
async def watchlist_delete(
    watchlist_id: int,
    request: Request,
    s: Session = Depends(get_session),
):
    remove_from_watchlist(watchlist_id, session=s)
    return RedirectResponse(url="/watchlist", status_code=303)


@router.post("/{watchlist_id}/target")
async def watchlist_set_target(
    watchlist_id: int,
    request: Request,
    target_price: str = Form(""),
    target_direction: str = Form(""),
    s: Session = Depends(get_session),
):
    try:
        if not target_price.strip():
            set_target(watchlist_id, None, None, session=s)
        else:
            tp = Decimal(target_price.strip())
            if target_direction not in {"above", "below"}:
                raise ValueError("target_direction must be 'above' or 'below'")
            set_target(watchlist_id, tp, target_direction, session=s)  # type: ignore[arg-type]
    except (ValueError, InvalidOperation) as exc:
        return RedirectResponse(
            url=f"/watchlist?err={str(exc).replace(' ', '+')}", status_code=303
        )
    return RedirectResponse(url="/watchlist", status_code=303)


@router.post("/{watchlist_id}/toggle-alert")
async def watchlist_toggle_alert(
    watchlist_id: int,
    request: Request,
    enabled: str = Form("off"),
    s: Session = Depends(get_session),
):
    toggle_alert(watchlist_id, enabled.lower() in {"on", "true", "1"}, session=s)
    return RedirectResponse(url="/watchlist", status_code=303)
