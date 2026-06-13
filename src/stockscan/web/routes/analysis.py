"""Per-symbol technical-analysis routes.

  GET  /analysis                          - listing: every watched symbol with summary card + mini chart.
  GET  /analysis/{symbol}                 - detail: large chart + full breakdown.
  POST /analysis/{symbol}/refresh-insider - on-demand insider pull (10 API credits; 23h cooldown).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from markupsafe import Markup
from sqlalchemy.orm import Session

from datetime import datetime as _datetime
from datetime import timedelta as _timedelta
from datetime import timezone as _tz

from stockscan.analysis import (
    analyze_symbol,
    analyze_watchlist_cards,
    build_chart_payload,
    render_chart_svg,
)
from stockscan.config import settings
from stockscan.data.providers.eodhd import EODHDError, EODHDProvider
from stockscan.earnings import latest_trend, next_earnings
from stockscan.econ_events import upcoming_events
from stockscan.insider import (
    net_buys_90d,
    recent_transactions,
    refresh_insider_for_symbol,
)
from stockscan.watchlist.store import list_watchlists, resolve_selection
from stockscan.web.deps import flash_redirect, get_session, render

router = APIRouter(prefix="/analysis")
log = logging.getLogger(__name__)


@router.get("")
@router.get("/")
async def analysis_list(
    request: Request,
    list: str | None = Query(None),
    s: Session = Depends(get_session),
):
    """Render the analysis hub: every watched symbol's analysis.

    ``?list=<id|all>`` selects which list to analyse (defaults to the primary
    "Watchlist" list). Each card carries its ``ohlc_history`` so the template
    can render an interactive candlestick chart with client-side time-window
    switching; the static SVG is kept as a no-bars fallback.
    """
    selected_id, selected_label = resolve_selection(list, session=s)
    lists = list_watchlists(session=s)
    raw_cards = analyze_watchlist_cards(list_id=selected_id, session=s)
    # Each card carries the full interactive-chart payload (every study, S/R
    # levels, expected-move bands, fib) so the hub charts have parity with the
    # detail page. The static SVG is kept as a no-bars fallback.
    cards = []
    for c in raw_cards:
        a = c["analysis"]
        cards.append({
            "analysis": a,
            "chart_svg": Markup(render_chart_svg(a, height=180)),
            "payload": c["payload"],
        })
    # Bars-as-of: the most recent close timestamp across every analysis. Each
    # SymbolAnalysis's closes_history is chronological, so its last tuple is
    # the latest local bar. Taking the max across the bundle is the right
    # "data freshness" signal — if any name lags, the user sees the lag.
    bars_as_of = None
    for c in cards:
        a = c["analysis"]
        if a.closes_history:
            d = a.closes_history[-1][0]
            if bars_as_of is None or d > bars_as_of:
                bars_as_of = d
    return render(
        request,
        "analysis/list.html",
        cards=cards,
        lists=lists,
        selected_id=selected_id,
        selected_label=selected_label,
        bars_as_of=bars_as_of,
    )


@router.get("/{symbol}")
async def analysis_detail(
    symbol: str,
    request: Request,
    s: Session = Depends(get_session),
):
    """Single-symbol detail view with a large interactive chart + breakdown.

    The chart is Lightweight-Charts-driven and the data is pre-computed for
    every available study so toggles never round-trip. The static-SVG
    fallback is kept as a safety net for when the analysis itself failed
    (no bars in store) — the template chooses based on
    ``chart_payload.bars``.
    """
    sym = symbol.upper().strip()
    if not sym:
        raise HTTPException(status_code=400, detail="empty symbol")
    analysis = analyze_symbol(sym, session=s)
    chart_payload = build_chart_payload(sym, analysis, session=s)
    # SVG fallback only used when the interactive payload is empty
    # (no bars in store yet for this symbol). Cheap to render.
    chart_svg = Markup(render_chart_svg(analysis, width=1100, height=380))
    # High-importance US macro events in the next 7 days — surfaced as a
    # small badge above the chart so entry-timing decisions account for
    # known vol catalysts.
    now_utc = _datetime.now(_tz.utc)
    try:
        macro_events = upcoming_events(
            start=now_utc,
            end=now_utc + _timedelta(days=7),
            country="US",
            importance_min="high",
            session=s,
        )
    except Exception:  # the badge is decorative — never block the page
        log.exception("analysis_detail: upcoming_events failed for %s", sym)
        macro_events = []

    # Next earnings + full per-period estimate trends — surfaced as a
    # "Estimate revisions" card under the existing trend/vol/momentum trio.
    try:
        upcoming_earn = next_earnings(sym, session=s)
    except Exception:
        log.exception("analysis_detail: next_earnings failed for %s", sym)
        upcoming_earn = None
    try:
        # Hide rows for periods that ended more than a year ago — those
        # are historical residue, not actionable for forward planning.
        from datetime import date as _date_anal, timedelta as _td_anal
        trends = latest_trend(
            sym,
            since=_date_anal.today() - _td_anal(days=365),
            session=s,
        )
    except Exception:
        log.exception("analysis_detail: latest_trend failed for %s", sym)
        trends = []

    # Insider transactions — recent 90 days + aggregated net buys.
    try:
        insider_txns = recent_transactions(sym, lookback_days=90, limit=10, session=s)
    except Exception:
        log.exception("analysis_detail: recent_transactions failed for %s", sym)
        insider_txns = []
    try:
        insider_summary = net_buys_90d(sym, session=s)
    except Exception:
        log.exception("analysis_detail: net_buys_90d failed for %s", sym)
        insider_summary = None
    return render(
        request,
        "analysis/detail.html",
        analysis=analysis,
        chart_svg=chart_svg,
        chart_payload=chart_payload,
        macro_events=macro_events,
        upcoming_earnings=upcoming_earn,
        earnings_trends=trends,
        insider_txns=insider_txns,
        insider_summary=insider_summary,
    )


@router.post("/{symbol}/refresh-insider")
async def analysis_refresh_insider(
    symbol: str,
    request: Request,
    s: Session = Depends(get_session),
):
    """On-demand per-symbol insider refresh from the analysis page.

    Costs 10 API credits per successful call — gated by the 23h
    per-symbol cooldown in ``insider_refresh_log``. The cooldown
    survives app restarts and page reloads because the timestamp lives
    in the DB, not in process memory.
    """
    sym = symbol.upper().strip()
    if not sym:
        raise HTTPException(status_code=400, detail="empty symbol")
    api_key = settings.eodhd_api_key.get_secret_value()
    if not api_key:
        return flash_redirect(
            f"/analysis/{sym}",
            "error",
            "EODHD_API_KEY is not set; can't refresh insider data.",
        )
    try:
        with EODHDProvider(api_key=api_key) as provider:
            result = refresh_insider_for_symbol(provider, sym, session=s)
    except EODHDError as exc:
        return flash_redirect(f"/analysis/{sym}", "error", f"Provider error: {exc}")
    except Exception as exc:  # safety
        log.exception("analysis_refresh_insider: unexpected error")
        return flash_redirect(f"/analysis/{sym}", "error", f"Refresh failed: {exc}")

    if result.skipped:
        h = (result.cooldown_remaining_secs or 0) / 3600.0
        return flash_redirect(
            f"/analysis/{sym}",
            "warn",
            f"Insider data was refreshed recently — try again in {h:.1f}h",
        )
    if result.error:
        return flash_redirect(f"/analysis/{sym}", "warn", f"Insider refresh: {result.error}")
    return flash_redirect(
        f"/analysis/{sym}",
        "success",
        f"Insider refreshed — {result.transactions_upserted} transaction(s) updated",
    )
