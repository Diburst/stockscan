"""Per-symbol technical-analysis routes.

  GET  /analysis                          - listing: every watched symbol with summary card + mini chart.
  GET  /analysis/{symbol}                 - detail: large chart + full breakdown.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from markupsafe import Markup
from sqlalchemy.orm import Session


from stockscan.analysis import (
    analyze_symbol,
    analyze_watchlist_cards,
    build_chart_payload,
    render_chart_svg,
)
from stockscan.earnings import latest_trend, next_earnings
from stockscan.insider import net_buys_90d, recent_transactions
from stockscan.watchlist.store import list_watchlists, resolve_selection
from stockscan.web.deps import get_session, render

router = APIRouter(prefix="/analysis")
log = logging.getLogger(__name__)


@router.get("")
@router.get("/")
def analysis_list(
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
    # Each card carries the full interactive-chart payload (every study +
    # expected-move bands) so the hub charts have parity with the detail
    # page. The static SVG is kept as a no-bars fallback.
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
def analysis_detail(
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
    # Next earnings + full per-period estimate trends — surfaced as a
    # "Estimate revisions" card under the existing trend/vol pair.
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
        upcoming_earnings=upcoming_earn,
        earnings_trends=trends,
        insider_txns=insider_txns,
        insider_summary=insider_summary,
    )
