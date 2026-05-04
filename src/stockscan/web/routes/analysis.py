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
    return render(
        request,
        "analysis/list.html",
        cards=cards,
    )


@router.get("/{symbol}")
async def analysis_detail(
    symbol: str,
    request: Request,
    s: Session = Depends(get_session),
):
    """Single-symbol detail view with a large chart + full breakdown."""
    sym = symbol.upper().strip()
    if not sym:
        raise HTTPException(status_code=400, detail="empty symbol")
    analysis = analyze_symbol(sym, session=s)
    chart_svg = Markup(render_chart_svg(analysis, width=1100, height=380))
    return render(
        request,
        "analysis/detail.html",
        analysis=analysis,
        chart_svg=chart_svg,
    )
