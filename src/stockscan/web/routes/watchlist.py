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
from stockscan.web.deps import (
    flash_redirect,
    get_session,
    hx_toast_response,
    render,
    safe,
)

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
    one_year_ago = today.replace(year=today.year - 1)
    tech_scores: dict[str, float | None] = {}
    for item in items:
        bars = safe(
            lambda sym=item.symbol: get_bars(sym, one_year_ago, today, session=s),
            label=f"watchlist.get_bars[{item.symbol}]",
        )
        if bars is None or getattr(bars, "empty", True):
            tech_scores[item.symbol] = None
            continue
        result = safe(
            lambda b=bars: compute_technical_score(None, b, today),
            label=f"watchlist.compute_technical_score[{item.symbol}]",
        )
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
            # The HX-Trigger toast appears immediately without a page reload.
            return hx_toast_response(
                _WATCH_ERROR_SNIPPET,
                "error",
                f"Couldn't add {symbol.upper()}: {exc}",
                status_code=400,
            )
        return flash_redirect(
            f"/watchlist?err={str(exc).replace(' ', '+')}",
            "error",
            f"Couldn't add {symbol.upper()}: {exc}",
        )

    if _is_htmx(request):
        # Replace the form in-place; no page reload, no scroll jump.
        return hx_toast_response(
            _WATCHING_SNIPPET, "success", f"Added {symbol.upper()} to watchlist"
        )
    return flash_redirect(
        redirect_to, "success", f"Added {symbol.upper()} to watchlist"
    )


@router.post("/{watchlist_id}/delete")
async def watchlist_delete(
    watchlist_id: int,
    request: Request,
    s: Session = Depends(get_session),
):
    # Capture the symbol before deletion so the toast can be specific.
    symbol = None
    try:
        items = list_watchlist(session=s)
        for item in items:
            if item.watchlist_id == watchlist_id:
                symbol = item.symbol
                break
    except Exception:  # noqa: BLE001 - lookup is best-effort for the toast
        pass
    remove_from_watchlist(watchlist_id, session=s)
    msg = f"Removed {symbol} from watchlist" if symbol else "Removed from watchlist"
    return flash_redirect("/watchlist", "success", msg)


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
            return flash_redirect("/watchlist", "success", "Target cleared")
        tp = Decimal(target_price.strip())
        if target_direction not in {"above", "below"}:
            raise ValueError("target_direction must be 'above' or 'below'")
        set_target(watchlist_id, tp, target_direction, session=s)  # type: ignore[arg-type]
    except (ValueError, InvalidOperation) as exc:
        return flash_redirect(
            f"/watchlist?err={str(exc).replace(' ', '+')}",
            "error",
            f"Couldn't save target: {exc}",
        )
    return flash_redirect("/watchlist", "success", "Target saved")


@router.post("/{watchlist_id}/toggle-alert")
async def watchlist_toggle_alert(
    watchlist_id: int,
    request: Request,
    enabled: str = Form("off"),
    s: Session = Depends(get_session),
):
    is_on = enabled.lower() in {"on", "true", "1"}
    toggle_alert(watchlist_id, is_on, session=s)
    return flash_redirect(
        "/watchlist", "info", "Alert " + ("armed" if is_on else "disarmed")
    )
