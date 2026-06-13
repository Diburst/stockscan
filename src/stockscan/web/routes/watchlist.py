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
from stockscan.data.backfill import backfill_symbol
from stockscan.data.providers.eodhd import EODHDError, EODHDProvider
from stockscan.data.store import get_bars, latest_bar_date, latest_daily_bar_date
from stockscan.earnings import days_until, next_earnings, refresh_earnings, revision_summary
from stockscan.econ_events import refresh_economic_events
from stockscan.insider import net_buys_90d, refresh_insider_for_watchlist
from stockscan.refresh_log import mark_refreshed, refresh_due
from stockscan.scan import refresh_signals
from stockscan.strategies.reversal_swing import ReversalSwing
from stockscan.watchlist.store import (
    add_symbols,
    add_to_watchlist,
    create_watchlist,
    delete_watchlist,
    list_watchlist,
    list_watchlists,
    remove_from_list,
    remove_from_watchlist,
    rename_watchlist,
    resolve_selection,
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

# ~1140 calendar days ≈ 3 years of trading bars. EOD history is a single
# provider call regardless of range, so we pull the full window the charts can
# display (the Analysis 3y button + 756-bar chart cap) up front. Comfortably
# covers the 252-bar (52-week) lookbacks the watchlist technical score needs.
_BACKFILL_CALENDAR_DAYS = 1140


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
    list: str | None = Query(None),
    s: Session = Depends(get_session),
):
    # Resolve which list is selected (defaults to the primary "Watchlist"
    # list; ``?list=all`` shows every symbol across all lists).
    selected_id, selected_label = resolve_selection(list, session=s)
    lists = list_watchlists(session=s)
    items = list_watchlist(list_id=selected_id, session=s)
    # Show each watched symbol's reversal_swing score on the fly. The watchlist
    # is small (~20-50 names) and bars are already local, so this is fast
    # (sub-100ms typical). We go through the strategy class because the strategy
    # is the only home for its scoring — same code path the scanner uses.
    today = _date.today()
    one_year_ago = today.replace(year=today.year - 1)
    revsw = ReversalSwing()
    tech_scores: dict[str, float | None] = {}
    # Per-symbol earnings + revisions + insider enrichment. All safe() —
    # never blocks the page; missing data renders as "—".
    next_earnings_by_sym: dict[str, object] = {}
    days_to_earn_by_sym: dict[str, int | None] = {}
    revisions_by_sym: dict[str, object] = {}
    insider_by_sym: dict[str, object] = {}
    for item in items:
        bars = safe(
            lambda sym=item.symbol: get_bars(sym, one_year_ago, today, session=s),
            label=f"watchlist.get_bars[{item.symbol}]",
        )
        if bars is None or getattr(bars, "empty", True):
            tech_scores[item.symbol] = None
        else:
            result = safe(
                lambda b=bars: revsw.reversal_score(b, today),
                label=f"watchlist.reversal_score[{item.symbol}]",
            )
            tech_scores[item.symbol] = result.score if result is not None else None

        earn = safe(
            lambda sym=item.symbol: next_earnings(sym, as_of=today, session=s),
            label=f"watchlist.next_earnings[{item.symbol}]",
        )
        next_earnings_by_sym[item.symbol] = earn
        days_to_earn_by_sym[item.symbol] = (
            days_until(earn.report_date, today) if earn is not None else None
        )
        # Filter to periods ending within the last 12 months so the
        # column reflects the current forward-looking quarter, not a
        # stale snapshot for a quarter that ran years ago.
        revisions_by_sym[item.symbol] = safe(
            lambda sym=item.symbol: revision_summary(
                sym,
                since=today - _timedelta(days=365),
                session=s,
            ),
            label=f"watchlist.revision_summary[{item.symbol}]",
        )
        insider_by_sym[item.symbol] = safe(
            lambda sym=item.symbol: net_buys_90d(sym, session=s),
            label=f"watchlist.net_buys_90d[{item.symbol}]",
        )
    return render(
        request,
        "watchlist/list.html",
        items=items,
        lists=lists,
        selected_id=selected_id,
        selected_label=selected_label,
        tech_scores=tech_scores,
        next_earnings_by_sym=next_earnings_by_sym,
        days_to_earn_by_sym=days_to_earn_by_sym,
        revisions_by_sym=revisions_by_sym,
        insider_by_sym=insider_by_sym,
        bars_as_of=_bars_as_of(items),
        err=err,
    )


# How many calendar days to backfill in the per-symbol watchlist pass.
# Matches the bulk refresh window (days_back=7) so the two passes cover the
# same range. The per-symbol path is idempotent (it checks ``latest_bar_date``
# and only fetches gaps) so a few extra days of overlap cost nothing.
_WATCHLIST_REFRESH_DAYS = 7

# Daily-ish cooldown for the slow-moving auxiliary fetches (economic-events
# calendar, earnings calendar/trends). 20h ≈ "once a day" while still allowing
# a same-day re-pull the next morning. Insider has its own 23h gate.
_AUX_COOLDOWN_HOURS = 20


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

      2. **Per-symbol backfill** for the names phase 1 missed. The bulk
         endpoint is exchange-scoped (``US``), so non-US names and freshly
         added watchlist symbols without prior history aren't covered by
         phase 1. Phase 2 only fetches symbols whose latest stored bar is
         behind the freshest market-wide day (``latest_daily_bar_date``);
         names the bulk pass already brought current are skipped with no API
         call. (Previously this re-fetched every watched name every refresh,
         because ``backfill_symbol`` re-pulls its overlap window even when
         current.)

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
    econ_upserted = 0
    econ_skipped_cooldown = False
    earnings_calendar_upserted = 0
    earnings_trends_upserted = 0
    earnings_skipped_cooldown = False
    insider_skipped_cooldown_h: float | None = None
    insider_symbols_refreshed = 0
    insider_transactions_upserted = 0
    try:
        with EODHDProvider(api_key=api_key) as provider:
            # ---- Phase 1: bulk S&P 500 + watchlist via the universe filter ----
            result = refresh_signals(
                provider, days_back=_WATCHLIST_REFRESH_DAYS, session=s
            )

            # ---- Phase 2: per-symbol catch-up for names the bulk pass missed ----
            # The bulk endpoint is US-exchange-scoped, so non-US names and
            # freshly-added symbols without prior history aren't covered by
            # phase 1. Rather than re-fetch EVERY watched name (backfill_symbol
            # re-pulls its overlap window even when current — one API call per
            # symbol), we only backfill names whose latest stored bar is behind
            # the freshest day the bulk pass just produced. Current US names are
            # skipped entirely (zero API calls). Soft-fail per symbol.
            watched = watchlist_symbols(session=s)
            cutoff = latest_daily_bar_date(session=s)  # freshest market-wide bar
            start = _date.today() - _timedelta(days=_WATCHLIST_REFRESH_DAYS)
            for sym in sorted(watched):
                lb = latest_bar_date(sym, session=s)
                if cutoff is not None and lb is not None and lb >= cutoff:
                    continue  # already current via the bulk pass — no fetch
                try:
                    watchlist_backfilled += backfill_symbol(provider, sym, start=start)
                except Exception as exc:
                    log.warning(
                        "watchlist refresh: per-symbol backfill failed for %s: %s",
                        sym, exc,
                    )
                    per_symbol_failures.append(sym)

            # ---- Phase 3: economic events (1 API call, US-only) ----
            # Daily cooldown: the macro calendar barely moves intraday, so
            # repeat refreshes within the window make no call.
            if refresh_due("econ_events", cooldown_hours=_AUX_COOLDOWN_HOURS, session=s):
                try:
                    econ_result = refresh_economic_events(provider, session=s)
                    econ_upserted = econ_result.upserted
                    if econ_result.error:
                        log.warning("watchlist refresh: econ_events: %s", econ_result.error)
                    else:
                        mark_refreshed("econ_events", session=s)
                except Exception as exc:  # safety
                    log.warning("watchlist refresh: econ_events fan-out: %s", exc)
            else:
                econ_skipped_cooldown = True

            # ---- Phase 4: earnings calendar + trends for watchlist names ----
            # Daily cooldown as well — estimate revisions update at most daily.
            watched_sorted = sorted(watched)
            if watched_sorted and refresh_due(
                "earnings", cooldown_hours=_AUX_COOLDOWN_HOURS, session=s
            ):
                try:
                    earn_result = refresh_earnings(
                        provider, watched_sorted, session=s,
                    )
                    earnings_calendar_upserted = earn_result.calendar_upserted
                    earnings_trends_upserted = earn_result.trends_upserted
                    if earn_result.error:
                        log.warning(
                            "watchlist refresh: earnings: %s", earn_result.error,
                        )
                    else:
                        mark_refreshed("earnings", session=s)
                except Exception as exc:
                    log.warning("watchlist refresh: earnings fan-out: %s", exc)
            elif watched_sorted:
                earnings_skipped_cooldown = True

            # ---- Phase 5: insider transactions (10 API calls per symbol!) ----
            # Gated by 23h cooldown via insider_refresh_log so this only
            # actually pulls once per day regardless of refresh frequency.
            try:
                ins_result = refresh_insider_for_watchlist(
                    provider, watched_sorted, session=s,
                )
                if ins_result.skipped:
                    insider_skipped_cooldown_h = (
                        (ins_result.cooldown_remaining_secs or 0) / 3600.0
                    )
                else:
                    insider_symbols_refreshed = ins_result.symbols_refreshed
                    insider_transactions_upserted = ins_result.transactions_upserted
                    if ins_result.error:
                        log.warning("watchlist refresh: insider: %s", ins_result.error)
            except Exception as exc:
                log.warning("watchlist refresh: insider fan-out: %s", exc)
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

    # Did any phase actually hit the provider? If the bars were already current
    # AND every auxiliary phase was cooldown-gated, the whole refresh was a
    # zero-API-cost no-op — say so plainly instead of "0 bars; 0 signals…".
    fetched_anything = any([
        result.bars_upserted, watchlist_backfilled, econ_upserted,
        earnings_calendar_upserted, earnings_trends_upserted,
        insider_symbols_refreshed,
    ])
    if not fetched_anything and result.up_to_date and not per_symbol_failures:
        notes: list[str] = []
        if econ_skipped_cooldown or earnings_skipped_cooldown:
            notes.append("macro/earnings on daily cooldown")
        if insider_skipped_cooldown_h is not None:
            notes.append(f"insider cooldown {insider_skipped_cooldown_h:.1f}h")
        suffix = (" (" + "; ".join(notes) + ")") if notes else ""
        return flash_redirect(
            "/analysis", "info", f"Already up to date — no API calls used{suffix}"
        )

    if result.up_to_date:
        msg_parts = ["bars already current"]
    else:
        msg_parts = [
            f"{result.bars_upserted} bulk bar(s) across "
            f"{result.bars_days_covered} day(s)",
            f"{result.signals_emitted} signal(s) across "
            f"{result.strategies_run} strategy(ies)",
        ]
    if watchlist_backfilled:
        msg_parts.append(f"{watchlist_backfilled} extra watchlist bar(s)")
    if econ_upserted:
        msg_parts.append(f"{econ_upserted} econ event(s)")
    if earnings_calendar_upserted or earnings_trends_upserted:
        msg_parts.append(
            f"{earnings_calendar_upserted} earnings + "
            f"{earnings_trends_upserted} trend point(s)"
        )
    if insider_symbols_refreshed:
        msg_parts.append(
            f"insider: {insider_transactions_upserted} txn(s) across "
            f"{insider_symbols_refreshed} sym"
        )
    elif insider_skipped_cooldown_h is not None:
        msg_parts.append(
            f"insider: skipped (cooldown {insider_skipped_cooldown_h:.1f}h remaining)"
        )
    if per_symbol_failures:
        msg_parts.append(
            f"{len(per_symbol_failures)} watchlist fetch(es) failed "
            f"({', '.join(per_symbol_failures[:3])}{'…' if len(per_symbol_failures) > 3 else ''})"
        )
    msg = "Refresh complete — " + "; ".join(msg_parts)
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
    list_id: str = Form(""),
    new_list_name: str = Form(""),
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
    msg = f"Added {sym} to watchlist{backfill_suffix}"
    kind = "warn" if "failed" in backfill_suffix else "success"

    if _is_htmx(request):
        # Replace the form in-place; no page reload, no scroll jump.
        return hx_toast_response(_WATCHING_SNIPPET, kind, msg)
    return flash_redirect(redirect_to, kind, msg)


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
async def watchlist_add_bulk(
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
async def watchlist_export(
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


@router.post("/lists/create")
async def watchlist_create_list(
    request: Request,
    name: str = Form(...),
    s: Session = Depends(get_session),
):
    try:
        lid = create_watchlist(name, session=s)
    except ValueError as exc:
        return flash_redirect("/watchlist", "error", f"Couldn't create list: {exc}")
    return flash_redirect(f"/watchlist?list={lid}", "success", f"Created list “{name.strip()}”")


@router.post("/lists/rename")
async def watchlist_rename_list(
    request: Request,
    list_id: str = Form(...),
    name: str = Form(...),
    s: Session = Depends(get_session),
):
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
async def watchlist_delete(
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
        msg = f"Removed {symbol} from this list" if symbol else "Removed from list"
        redirect = f"/watchlist?list={lid}"
    else:
        remove_from_watchlist(watchlist_id, session=s)
        msg = f"Removed {symbol} from watchlist" if symbol else "Removed from watchlist"
        redirect = "/watchlist?list=all"
    return flash_redirect(redirect, "success", msg)


@router.post("/lists/delete")
async def watchlist_delete_list(
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
    return flash_redirect("/watchlist?list=all", "success", "List deleted")


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
