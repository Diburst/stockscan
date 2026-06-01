"""Per-symbol technical-analysis routes.

  GET /analysis           - listing: every watched symbol with summary card + mini chart.
  GET /analysis/{symbol}  - detail: large chart + full breakdown.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from markupsafe import Markup
from sqlalchemy.orm import Session

from stockscan.analysis import (
    analyze_symbol,
    analyze_watchlist,
    build_chart_payload,
    render_chart_svg,
)
from stockscan.web.deps import get_session, render

router = APIRouter(prefix="/analysis")
log = logging.getLogger(__name__)


@router.get("")
@router.get("/")
async def analysis_list(request: Request, s: Session = Depends(get_session)):
    """Render the analysis hub: every watched symbol's analysis."""
    analyses = analyze_watchlist(session=s)
    # Pre-render mini-charts so the template doesn't need the chart
    # module imported.
    cards = []
    for a in analyses:
        cards.append({
            "analysis": a,
            "chart_svg": Markup(render_chart_svg(a, height=180)),
        })
    # Bars-as-of: the most recent close timestamp across every analysis. Each
    # SymbolAnalysis's closes_history is chronological, so its last tuple is
    # the latest local bar. Taking the max across the bundle is the right
    # "data freshness" signal — if any name lags, the user sees the lag.
    bars_as_of = None
    for a in analyses:
        if a.closes_history:
            d = a.closes_history[-1][0]
            if bars_as_of is None or d > bars_as_of:
                bars_as_of = d
    return render(
        request,
        "analysis/list.html",
        cards=cards,
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
    return render(
        request,
        "analysis/detail.html",
        analysis=analysis,
        chart_svg=chart_svg,
        chart_payload=chart_payload,
    )
