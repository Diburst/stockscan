"""Watchlist page — list, add, remove, set/clear target, toggle alert."""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Literal

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from datetime import date as _date, timedelta as _timedelta

from stockscan.config import settings
from stockscan.data.backfill import CATCH_UP_CALENDAR_DAYS, backfill_symbol
from stockscan.data.providers.eodhd import EODHDProvider
from stockscan.watchlist.composite import (
    composite_payload,
    member_series,
    rebuild_for_symbol,
    refresh_watchlist_composites,
)
from stockscan.watchlist.store import (
    add_symbols,
    add_to_watchlist,
    create_watchlist,
    delete_watchlist,
    list_watchlist,
    list_watchlists,
    lists_for_symbol,
    remove_from_list,
    remove_from_watchlist,
    remove_symbol,
    rename_watchlist,
    resolve_selection,
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

log = logging.getLogger(__name__)

# EOD history is a single provider call regardless of range, so a new name
# gets the full window the charts can display up front.
_BACKFILL_CALENDAR_DAYS = CATCH_UP_CALENDAR_DAYS


def _backfill_history(symbol: str) -> str:
    """Best-effort one-time historical backfill for a newly-watched symbol.

    Runs synchronously inside the Add request so the Analysis page and
    technical score work immediately rather than filling in over months
    of daily refreshes. Returns a short suffix for the success toast
    (e.g., " — 271 bars backfilled"); never raises — a provider failure
    just means the symbol is added without history and the next Refresh
    will start catching it up.
    """
    api_key = settings.eodhd_api_key.get_secret_value()
    if not api_key:
        return " (set EODHD_API_KEY to fetch price history)"

    start = _date.today() - _timedelta(days=_BACKFILL_CALENDAR_DAYS)

    def _run() -> int:
        with EODHDProvider(api_key=api_key) as provider:
            return backfill_symbol(provider, symbol, start=start)

    n_bars = safe(_run, label=f"watchlist.backfill[{symbol}]")
    if n_bars is None:
        return " — history backfill failed (see logs); next Refresh will retry"
    if n_bars > 0:
        return f" — {n_bars} bars backfilled"
    return ""  # already up to date — no need to mention it

router = APIRouter(prefix="/watchlist")


def _rebuild_composites(list_id: int | None, s: Session) -> None:
    """Rebuild a list's composites after a membership change. Best-effort: it
    reads only stored bars + stored share history (no API calls) and must never
    break the user's add/remove action, so failures are logged and swallowed."""
    if list_id is None:
        return
    safe(
        lambda: refresh_watchlist_composites(list_id, session=s),
        label=f"watchlist.rebuild_composites[{list_id}]",
    )


def _bars_as_of(items: list) -> _date | None:
    """Latest bar date seen across the watchlist (i.e. data freshness)."""
    dates = [it.last_bar_date for it in items if it.last_bar_date is not None]
    if not dates:
        return None
    latest = max(dates)
    # `last_bar_date` is a datetime in UTC on the WatchlistItem; coerce to date.
    return latest.date() if hasattr(latest, "date") else latest


@router.get("")
def watchlist_list(
    request: Request,
    err: str | None = Query(None),
    list: str | None = Query(None),
    s: Session = Depends(get_session),
):
    """The watchlist page for the selected list (``?list=`` id or 'all').
    Per-symbol earnings, revisions and insider detail live on the Analysis
    page each symbol links to."""
    selected_id, selected_label = resolve_selection(list, session=s)
    lists = list_watchlists(session=s)
    items = list_watchlist(list_id=selected_id, session=s)
    return render(
        request,
        "watchlist/list.html",
        items=items,
        lists=lists,
        selected_id=selected_id,
        selected_label=selected_label,
        bars_as_of=_bars_as_of(items),
        err=err,
    )


# Inline replacement returned to HTMX requests after a successful add — the
# Dashboard's "+ Watch" button is replaced with this in-place (no page reload).
_WATCH_ERROR_SNIPPET = (
    '<span class="text-xs px-2 py-1 rounded bg-bad-100 text-bad-600 '
    'border border-bad-600/30 inline-block">✗ error</span>'
)


def _watching_snippet(symbol: str, redirect_to: str = "/") -> str:
    """The '✓ watching' pill — a click-to-unwatch HTMX toggle.

    Returned by POST /watchlist/add (HX path) and rendered statically by
    the Dashboard's ``watch_button`` macro; the two MUST stay visually
    identical so the pill looks the same pre- and post-click.
    """
    return (
        '<form action="/watchlist/unwatch" method="post" class="inline"'
        ' hx-post="/watchlist/unwatch" hx-target="this" hx-swap="outerHTML"'
        ' hx-disabled-elt="find button">'
        f'<input type="hidden" name="symbol" value="{symbol}">'
        f'<input type="hidden" name="redirect_to" value="{redirect_to}">'
        '<button type="submit" class="text-xs px-2 py-1 rounded bg-ok-100'
        ' text-ok-600 border border-ok-600/30 hover:bg-bad-100'
        ' hover:text-bad-600 hover:border-bad-600/30 disabled:opacity-50'
        ' disabled:cursor-wait"'
        f' title="Click to remove {symbol} from the watchlist">'
        "✓ watching</button></form>"
    )


def _watch_form_snippet(symbol: str, redirect_to: str = "/") -> str:
    """The '+ Watch' quick-add form — what the unwatch toggle swaps back to.

    Mirrors the Dashboard ``watch_button`` macro's else-branch markup.
    """
    return (
        '<form action="/watchlist/add" method="post" class="inline"'
        ' hx-post="/watchlist/add" hx-target="this" hx-swap="outerHTML"'
        ' hx-disabled-elt="find button">'
        f'<input type="hidden" name="symbol" value="{symbol}">'
        f'<input type="hidden" name="redirect_to" value="{redirect_to}">'
        '<button type="submit" class="text-xs px-2 py-1 rounded border'
        ' border-ink-300 hover:bg-ink-100 disabled:opacity-50 disabled:cursor-wait"'
        f' title="Add {symbol} to watchlist (fetches ~1 year of price history)">'
        "+ Watch</button></form>"
    )


def _is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request", "").lower() == "true"


@router.post("/add")
def watchlist_add(
    request: Request,
    symbol: str = Form(..., min_length=1, max_length=10),
    target_price: str = Form(""),
    target_direction: str = Form(""),
    note: str = Form(""),
    list_id: str = Form(""),
    new_list_name: str = Form(""),
    redirect_to: str = Form("/watchlist"),
    s: Session = Depends(get_session),
):
    """Add a symbol to a list (optionally creating the list) with an optional
    target price/direction and note, then synchronously backfill ~3 years of
    history best-effort. HTMX requests get the in-place 'watching' pill swap;
    plain posts redirect with a flash toast."""
    try:
        tp: Decimal | None = None
        td: Literal["above", "below"] | None = None
        if target_price.strip():
            tp = Decimal(target_price.strip())
        if target_direction.strip():
            if target_direction not in {"above", "below"}:
                raise ValueError("target_direction must be 'above' or 'below'")
            td = target_direction  # type: ignore[assignment]
        # A blank / "all" list_id with no new-list name falls through to the
        # store's default-list resolution.
        lid: int | None = None
        if list_id.strip() and list_id.strip().lower() != "all":
            try:
                lid = int(list_id)
            except ValueError:
                lid = None
        add_to_watchlist(
            symbol,
            target_price=tp,
            target_direction=td,
            note=note.strip() or None,
            list_id=lid,
            new_list_name=new_list_name.strip() or None,
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

    # One-time historical backfill so the symbol shows up on the watchlist
    # AND in Analysis right away (not after months of 7-day refreshes).
    # Best-effort: the symbol is already added at this point.
    sym = symbol.strip().upper()
    backfill_suffix = _backfill_history(sym)
    # Membership changed → rebuild the composites for every list the symbol is
    # now on (covers the just-added list and any others it already belonged to).
    safe(lambda: rebuild_for_symbol(sym, session=s), label=f"watchlist.rebuild[{sym}]")
    msg = f"Added {sym} to watchlist{backfill_suffix}"
    kind = "warn" if "failed" in backfill_suffix else "success"

    if _is_htmx(request):
        # Replace the form in-place; no page reload, no scroll jump.
        return hx_toast_response(_watching_snippet(sym, redirect_to), kind, msg)
    return flash_redirect(redirect_to, kind, msg)


@router.post("/unwatch")
def watchlist_unwatch(
    request: Request,
    symbol: str = Form(..., min_length=1, max_length=10),
    redirect_to: str = Form("/"),
    s: Session = Depends(get_session),
):
    """Remove ``symbol`` from the watchlist (all lists) — the Dashboard
    pill's click-to-unwatch toggle.

    HX path swaps the pill back to the '+ Watch' form in place, so the
    Dashboard state flips without a reload (TODO.md 'pill auto-flip').
    """
    sym = symbol.strip().upper()
    # Capture the lists the symbol is on BEFORE removal (the membership rows are
    # gone afterwards) so we can rebuild each affected composite.
    affected = safe(lambda: lists_for_symbol(sym, session=s), default=[],
                    label=f"watchlist.lists_for[{sym}]") or []
    removed = remove_symbol(sym, session=s)
    for lid in affected:
        _rebuild_composites(lid, s)
    msg = (
        f"Removed {sym} from watchlist"
        if removed
        else f"{sym} wasn't on the watchlist"
    )
    if _is_htmx(request):
        return hx_toast_response(
            _watch_form_snippet(sym, redirect_to),
            "success" if removed else "info",
            msg,
        )
    return flash_redirect(redirect_to, "success" if removed else "info", msg)


def _resolve_lid_form(list_id: str) -> int | None:
    """Parse a form list_id value ('' / 'all' / int) into an optional id."""
    if list_id.strip() and list_id.strip().lower() != "all":
        try:
            return int(list_id)
        except ValueError:
            return None
    return None


def _backfill_many(symbols: list[str]) -> tuple[int, list[str]]:
    """Best-effort one-time history backfill for a batch of newly-added names.

    Opens a single provider session and pulls each symbol's history so charts
    and Analysis work immediately. Returns (total_bars, failed_symbols); never
    raises — a provider hiccup just means the next Refresh catches the gap.
    """
    if not symbols:
        return 0, []
    api_key = settings.eodhd_api_key.get_secret_value()
    if not api_key:
        return 0, list(symbols)  # no key → treat as "not fetched"
    start = _date.today() - _timedelta(days=_BACKFILL_CALENDAR_DAYS)
    total = 0
    failed: list[str] = []

    def _run() -> None:
        nonlocal total
        with EODHDProvider(api_key=api_key) as provider:
            for sym in symbols:
                try:
                    total += backfill_symbol(provider, sym, start=start)
                except Exception as exc:  # noqa: BLE001 - per-symbol soft-fail
                    log.warning("watchlist bulk backfill failed for %s: %s", sym, exc)
                    failed.append(sym)

    safe(_run, label="watchlist.add_bulk.backfill")
    return total, failed


@router.post("/add-bulk")
def watchlist_add_bulk(
    request: Request,
    symbols: str = Form(""),
    list_id: str = Form(""),
    new_list_name: str = Form(""),
    redirect_to: str = Form("/watchlist"),
    s: Session = Depends(get_session),
):
    """Add one or many tickers (comma / space / newline separated) to a list,
    then backfill price history for each so charts + Analysis work right away."""
    if not symbols.strip():
        return flash_redirect(redirect_to, "warn", "No symbols to add")
    try:
        result = add_symbols(
            symbols,
            list_id=_resolve_lid_form(list_id),
            new_list_name=new_list_name.strip() or None,
            session=s,
        )
    except ValueError as exc:
        return flash_redirect(redirect_to, "error", f"Couldn't add symbols: {exc}")

    # Commit the membership inserts before the (potentially slow) provider
    # fetch so the symbols are persisted even if backfill hiccups.
    try:
        s.commit()
    except Exception:  # the session dependency will also commit on success
        log.warning("watchlist add-bulk: pre-backfill commit failed", exc_info=True)

    bars, failed = _backfill_many(result.added)

    # One rebuild for the whole batch (not per-symbol) — the bulk-add path.
    _rebuild_composites(result.list_id, s)

    parts = [f"Added {len(result.added)} symbol(s)"]
    if bars:
        parts.append(f"{bars} bars backfilled")
    if failed:
        shown = ", ".join(failed[:3])
        more = "…" if len(failed) > 3 else ""
        parts.append(f"{len(failed)} history fetch(es) failed ({shown}{more})")
    if result.invalid:
        shown = ", ".join(result.invalid[:5])
        more = "…" if len(result.invalid) > 5 else ""
        parts.append(f"skipped {len(result.invalid)} invalid ({shown}{more})")
    kind = "warn" if (result.invalid or failed) else "success"
    # Land on the list the symbols went into so the user sees them.
    return flash_redirect(
        f"/watchlist?list={result.list_id}", kind, " — ".join(parts)
    )


@router.get("/export")
def watchlist_export(
    request: Request,
    list: str | None = Query(None),
    s: Session = Depends(get_session),
):
    """Export a list's symbols as a comma-separated plain-text block (for copy
    or download). ``?list=all`` exports every symbol across all lists."""
    selected_id, label = resolve_selection(list, session=s)
    items = list_watchlist(list_id=selected_id, session=s)
    body = ", ".join(it.symbol for it in items)
    filename = "watchlist-all.txt" if selected_id is None else f"watchlist-{label}.txt"
    return PlainTextResponse(
        body,
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


@router.get("/composite")
def watchlist_composite(
    list_id: int = Query(...),
    s: Session = Depends(get_session),
):
    """JSON: the two base-100 composite series (equal-weight + cap-weight) for a
    list, plus its member symbols and the freshness date. The chart rebases these
    to the selected window client-side. Soft-fails to an empty, unbuilt payload so
    the page still renders (with a Rebuild prompt) if anything goes wrong."""
    payload = safe(
        lambda: composite_payload(list_id, session=s),
        label=f"watchlist.composite[{list_id}]",
    )
    if payload is None:
        return {
            "list_id": list_id, "members": [], "built": False,
            "as_of": None, "series": {"equal_weight": [], "cap_weight": []},
        }
    return payload


@router.get("/series")
def watchlist_member_series(
    symbols: str = Query(...),
    s: Session = Depends(get_session),
):
    """JSON: absolute adjusted-close series for individual symbols, for overlay
    lines on the composite chart. Accepts a comma-separated ``symbols`` list;
    capped to a sane number so a crafted request can't fan out unbounded."""
    wanted = [t.strip().upper() for t in symbols.split(",") if t.strip()][:25]
    out = safe(
        lambda: member_series(wanted, session=s),
        default={},
        label="watchlist.member_series",
    )
    return {"series": out or {}}


@router.post("/lists/create")
def watchlist_create_list(
    request: Request,
    name: str = Form(...),
    s: Session = Depends(get_session),
):
    """Create a new named list and land on its view — a rejected name
    redirects back with an error toast."""
    try:
        lid = create_watchlist(name, session=s)
    except ValueError as exc:
        return flash_redirect("/watchlist", "error", f"Couldn't create list: {exc}")
    return flash_redirect(f"/watchlist?list={lid}", "success", f"Created list “{name.strip()}”")


@router.post("/lists/rename")
def watchlist_rename_list(
    request: Request,
    list_id: str = Form(...),
    name: str = Form(...),
    s: Session = Depends(get_session),
):
    """Rename a list, staying on its view — an invalid id or rejected name
    surfaces as an error toast."""
    try:
        lid = int(list_id)
    except ValueError:
        return flash_redirect("/watchlist", "error", "Invalid list")
    try:
        rename_watchlist(lid, name, session=s)
    except ValueError as exc:
        return flash_redirect(f"/watchlist?list={lid}", "error", f"Couldn't rename: {exc}")
    return flash_redirect(f"/watchlist?list={lid}", "success", "List renamed")


@router.post("/{watchlist_id}/delete")
def watchlist_delete(
    watchlist_id: int,
    request: Request,
    list_id: str = Query(""),
    s: Session = Depends(get_session),
):
    """Remove a symbol.

    On a specific-list view (``list_id`` set) this drops the symbol from that
    list only — and the store deletes the symbol entirely if it was its last
    list. On the "All" view (``list_id`` blank / "all") it deletes the symbol
    outright across every list.
    """
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

    lid: int | None = None
    if list_id.strip() and list_id.strip().lower() != "all":
        try:
            lid = int(list_id)
        except ValueError:
            lid = None

    if lid is not None:
        remove_from_list(watchlist_id, lid, session=s)
        _rebuild_composites(lid, s)
        msg = f"Removed {symbol} from this list" if symbol else "Removed from list"
        redirect = f"/watchlist?list={lid}"
    else:
        # Removing the symbol from every list — capture them first so each
        # affected composite is rebuilt after the rows are gone.
        affected = (
            safe(lambda: lists_for_symbol(symbol, session=s), default=[],
                 label="watchlist.lists_for_delete") or []
            if symbol else []
        )
        remove_from_watchlist(watchlist_id, session=s)
        for aff in affected:
            _rebuild_composites(aff, s)
        msg = f"Removed {symbol} from watchlist" if symbol else "Removed from watchlist"
        redirect = "/watchlist?list=all"
    return flash_redirect(redirect, "success", msg)


@router.post("/lists/delete")
def watchlist_delete_list(
    request: Request,
    list_id: str = Form(...),
    s: Session = Depends(get_session),
):
    """Delete an entire named list. Symbols left on no other list are removed."""
    try:
        lid = int(list_id)
    except ValueError:
        return flash_redirect("/watchlist", "error", "Invalid list")
    delete_watchlist(lid, session=s)
    # The list (and its memberships) are gone; rebuilding now finds no members
    # and clears the stale $WLEQ/$WLCW synthetic bars for this list.
    _rebuild_composites(lid, s)
    return flash_redirect("/watchlist?list=all", "success", "List deleted")


@router.post("/{watchlist_id}/target")
def watchlist_set_target(
    watchlist_id: int,
    request: Request,
    target_price: str = Form(""),
    target_direction: str = Form(""),
    s: Session = Depends(get_session),
):
    """Set or clear a row's target price — a blank price clears the target,
    otherwise the direction must be 'above' or 'below'. Validation errors
    redirect back with an error toast."""
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
def watchlist_toggle_alert(
    watchlist_id: int,
    request: Request,
    enabled: str = Form("off"),
    s: Session = Depends(get_session),
):
    """Arm or disarm a row's target alert from its checkbox form ('on' /
    'true' / '1' means armed) and redirect back with a toast."""
    is_on = enabled.lower() in {"on", "true", "1"}
    toggle_alert(watchlist_id, is_on, session=s)
    return flash_redirect(
        "/watchlist", "info", "Alert " + ("armed" if is_on else "disarmed")
    )
