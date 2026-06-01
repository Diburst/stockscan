"""Watchlist page — list, add, remove, set/clear target, toggle alert."""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Literal

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from datetime import date as _date, timedelta as _timedelta

from stockscan.config import settings
from stockscan.data.backfill import backfill_symbol
from stockscan.data.providers.eodhd import EODHDError, EODHDProvider
from stockscan.data.store import get_bars
from stockscan.scan import refresh_signals
from stockscan.strategies.reversal_swing import ReversalSwing
from stockscan.watchlist.store import (
    add_to_watchlist,
    list_watchlist,
    remove_from_watchlist,
    set_target,
    toggle_alert,
    watchlist_symbols,
)
from stockscan.web.deps import (
    flash_redirect,
    get_session,
    hx_toast_response,
    rate_limit_check,
    render,
    safe,
)

log = logging.getLogger(__name__)

# ~380 calendar days ≈ 270 trading days — comfortably covers the 252-bar
# (52-week) lookbacks the Analysis page and the watchlist technical score
# need, with margin for holidays.
_BACKFILL_CALENDAR_DAYS = 380


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


def _bars_as_of(items: list) -> _date | None:
    """Latest bar date seen across the watchlist (i.e. data freshness)."""
    dates = [it.last_bar_date for it in items if it.last_bar_date is not None]
    if not dates:
        return None
    latest = max(dates)
    # `last_bar_date` is a datetime in UTC on the WatchlistItem; coerce to date.
    return latest.date() if hasattr(latest, "date") else latest


@router.get("")
async def watchlist_list(
    request: Request,
    err: str | None = Query(None),
    s: Session = Depends(get_session),
):
    items = list_watchlist(session=s)
    # Show each watched symbol's reversal_swing score on the fly. The watchlist
    # is small (~20-50 names) and bars are already local, so this is fast
    # (sub-100ms typical). We go through the strategy class because the strategy
    # is the only home for its scoring — same code path the scanner uses.
    today = _date.today()
    one_year_ago = today.replace(year=today.year - 1)
    revsw = ReversalSwing()
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
            lambda b=bars: revsw.reversal_score(b, today),
            label=f"watchlist.reversal_score[{item.symbol}]",
        )
        tech_scores[item.symbol] = result.score if result is not None else None
    return render(
        request,
        "watchlist/list.html",
        items=items,
        tech_scores=tech_scores,
        bars_as_of=_bars_as_of(items),
        err=err,
    )


# How many calendar days to backfill in the per-symbol watchlist pass.
# Matches the bulk refresh window (days_back=7) so the two passes cover the
# same range. The per-symbol path is idempotent (it checks ``latest_bar_date``
# and only fetches gaps) so a few extra days of overlap cost nothing.
_WATCHLIST_REFRESH_DAYS = 7


@router.post("/refresh-bars")
async def watchlist_refresh_bars(
    request: Request,
    s: Session = Depends(get_session),
):
    """Pull fresh EOD bars for the S&P 500 universe + watchlist, re-run all
    strategies, then redirect to /analysis so the freshly-derived indicators
    are visible immediately.

    Two-phase fetch scope:

      1. **Bulk-EOD** over the S&P 500 universe ∪ watchlist (via
         ``refresh_signals``), pulled one trading day at a time on the US
         exchange. This covers the vast majority of names cheaply (one API
         call per day, not per symbol).

      2. **Per-symbol backfill** over every watchlist symbol. The bulk
         endpoint is exchange-scoped (``US``), so names listed on other
         exchanges — and freshly-added watchlist names without prior
         history — are NOT covered by phase 1. Phase 2 plugs that gap.
         ``backfill_symbol`` is idempotent: it reads ``latest_bar_date``
         and fetches only the missing window, so for names already updated
         in phase 1 this is a zero-cost no-op.

    Equivalent to ``stockscan refresh daily`` from the CLI plus the
    explicit watchlist coverage guarantee. Per the "indicators must follow
    bars" requirement, lazily-computed indicators on /watchlist and
    /analysis will pick up the new bars on the next page load — that's the
    redirect target.
    """
    cooldown_remaining = rate_limit_check("watchlist.refresh", cooldown_seconds=15)
    if cooldown_remaining is not None:
        return flash_redirect(
            "/analysis",
            "warn",
            f"Just refreshed — try again in {int(cooldown_remaining) + 1}s",
        )

    api_key = settings.eodhd_api_key.get_secret_value()
    if not api_key:
        return flash_redirect(
            "/watchlist",
            "error",
            "EODHD_API_KEY is not set. Add it to your .env to refresh bars.",
        )

    watchlist_backfilled = 0
    per_symbol_failures: list[str] = []
    try:
        with EODHDProvider(api_key=api_key) as provider:
            # ---- Phase 1: bulk S&P 500 + watchlist via the universe filter ----
            result = refresh_signals(
                provider, days_back=_WATCHLIST_REFRESH_DAYS, session=s
            )

            # ---- Phase 2: per-symbol catch-up for every watchlist name ----
            # Belt-and-suspenders. Catches non-US-exchange names and
            # freshly-added symbols whose bars never made it through the
            # bulk filter. Soft-fail per symbol — one bad ticker shouldn't
            # blank out the whole refresh.
            watched = watchlist_symbols(session=s)
            start = _date.today() - _timedelta(days=_WATCHLIST_REFRESH_DAYS)
            for sym in sorted(watched):
                try:
                    watchlist_backfilled += backfill_symbol(provider, sym, start=start)
                except Exception as exc:
                    log.warning(
                        "watchlist refresh: per-symbol backfill failed for %s: %s",
                        sym, exc,
                    )
                    per_symbol_failures.append(sym)
    except EODHDError as exc:
        log.warning("watchlist refresh: provider error: %s", exc)
        try:
            s.rollback()
        except Exception as roll_exc:
            log.warning("watchlist refresh: rollback failed: %s", roll_exc)
        return flash_redirect("/watchlist", "error", f"Provider error: {exc}")
    except Exception as exc:
        log.exception("watchlist refresh: unexpected error")
        try:
            s.rollback()
        except Exception as roll_exc:
            log.warning("watchlist refresh: rollback failed: %s", roll_exc)
        return flash_redirect("/watchlist", "error", f"Refresh failed: {exc}")

    msg_parts = [
        f"{result.bars_upserted} bulk bar(s) across {result.bars_days_covered} day(s)",
    ]
    if watchlist_backfilled:
        msg_parts.append(f"{watchlist_backfilled} extra watchlist bar(s)")
    msg_parts.append(
        f"{result.signals_emitted} signal(s) across "
        f"{result.strategies_run} strategy(ies)"
    )
    if per_symbol_failures:
        msg_parts.append(
            f"{len(per_symbol_failures)} watchlist fetch(es) failed "
            f"({', '.join(per_symbol_failures[:3])}{'…' if len(per_symbol_failures) > 3 else ''})"
        )
    msg = "Bars refreshed — " + "; ".join(msg_parts)
    kind = "warn" if (result.failures or per_symbol_failures) else "success"
    return flash_redirect("/analysis", kind, msg)


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

    # One-time historical backfill so the symbol shows up on the watchlist
    # AND in Analysis right away (not after months of 7-day refreshes).
    # Best-effort: the symbol is already added at this point.
    sym = symbol.strip().upper()
    backfill_suffix = _backfill_history(sym)
    msg = f"Added {sym} to watchlist{backfill_suffix}"
    kind = "warn" if "failed" in backfill_suffix else "success"

    if _is_htmx(request):
        # Replace the form in-place; no page reload, no scroll jump.
        return hx_toast_response(_WATCHING_SNIPPET, kind, msg)
    return flash_redirect(redirect_to, kind, msg)


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
