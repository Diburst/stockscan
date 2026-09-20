"""Signals page — passing + rejected with badges (USER_STORIES Story 1).

Endpoints:
  GET  /signals                    — full page render
  GET  /signals/{signal_id}        — detail view
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from stockscan.regime import get_regime
from stockscan.regime.store import MarketRegime
from stockscan.scan import signals_freshness
from stockscan.signals import SORT_COLUMNS, query_signals
from stockscan.strategies import (
    STRATEGY_REGISTRY,
    Strategy,
    discover_strategies,
)
from stockscan.web.deps import get_session, render, safe

router = APIRouter(prefix="/signals")


# Valid sort columns + their SQL expressions.
def _to_float(v: str | None) -> float | None:
    """Parse a query-string value as float, treating '' as None.

    HTML forms send empty number inputs as ``""``, which FastAPI can't
    coerce to ``float | None`` — it 422s. Accepting ``str | None`` and
    converting here avoids that.
    """
    if v is None or v.strip() == "":
        return None
    return float(v)


# Sort columns now live on the signals service (single home for the query
# contract); kept here only for the template's valid-sort-key echo.
_SORT_COLUMNS = SORT_COLUMNS


def _query_signals_view(
    s: Session,
    *,
    strategy: str | None,
    days: int,
    show_rejected: bool,
    # Filtering
    symbol: str | None = None,
    side: str | None = None,
    score_min: float | None = None,
    score_max: float | None = None,
    # Sorting
    sort: str | None = None,
    sort_dir: str | None = None,
) -> dict[str, Any]:
    """Bundle the template context for the signals list view.

    The SELECT itself lives in :func:`stockscan.signals.query_signals` so this
    page and the MCP ``list_signals`` tool share one query.
    This helper only splits passing/rejected and adds the template-specific
    bits (strategy registry, freshness, filter/sort echo state).
    """
    rows = query_signals(
        session=s,
        strategy=strategy,
        days=days,
        show_rejected=show_rejected,
        symbol=symbol,
        side=side,
        score_min=score_min,
        score_max=score_max,
        sort=sort,
        sort_dir=sort_dir,
    )
    sort_key = sort if sort in _SORT_COLUMNS else None
    passing = [r for r in rows if r.status == "new"]
    rejected = [r for r in rows if r.status == "rejected"]

    return {
        "passing": passing,
        "rejected": rejected,
        "strategies": STRATEGY_REGISTRY.all(),
        "active_strategy": strategy,
        "show_rejected": show_rejected,
        "days": days,
        "freshness": signals_freshness(session=s),
        # Filter state (so the template can preserve values)
        "filter_symbol": symbol or "",
        "filter_side": side or "",
        "filter_score_min": score_min,
        "filter_score_max": score_max,
        # Sort state
        "sort_key": sort_key or "",
        "sort_dir": sort_dir or "desc",
    }


@router.get("")
def signals_list(
    request: Request,
    strategy: str | None = Query(None),
    days: int = Query(7, ge=1, le=90),
    show_rejected: bool = Query(True),
    # Filters
    symbol: str | None = Query(None),
    side: str | None = Query(None),
    score_min: str | None = Query(None),
    score_max: str | None = Query(None),
    # Sort
    sort: str | None = Query(None),
    dir: str | None = Query(None),
    s: Session = Depends(get_session),
):
    """Full page render of passing + rejected signals (current strategy
    versions only), with symbol/side/score-range filters and column sorting
    via query params."""
    ctx = _query_signals_view(
        s,
        strategy=strategy,
        days=days,
        show_rejected=show_rejected,
        symbol=symbol,
        side=side,
        score_min=_to_float(score_min),
        score_max=_to_float(score_max),
        sort=sort,
        sort_dir=dir,
    )
    return render(
        request,
        "signals/list.html",
        **ctx,
    )


@router.get("/{signal_id}")
def signal_detail(
    signal_id: int,
    request: Request,
    s: Session = Depends(get_session),
):
    """Full attribution view: every input that produced this signal's score.

    Pulls together (1) the signal row + JSONB strategy metadata (which
    carries the strategy-owned score breakdown), (2) the regime row at
    the same as_of_date for the trend gate / vol scalar / credit-stress
    context, and (3) the strategy class itself for its sizing rule and
    the human-readable manual.

    Each lookup soft-fails to ``None`` so a missing row in any one
    table never blanks out the whole page.
    """
    discover_strategies()

    # ---- 1. The signal itself.
    sig_sql = text(
        """
        SELECT s.signal_id, s.strategy_name,
               s.strategy_version, s.symbol, s.side, s.score, s.status,
               s.as_of_date, s.suggested_entry, s.suggested_stop,
               s.suggested_target, s.suggested_qty, s.rejected_reason,
               s.metadata
        FROM signals s
        WHERE s.signal_id = :sid
        """
    )
    signal = s.execute(sig_sql, {"sid": signal_id}).first()
    if signal is None:
        return render(
            request,
            "signals/detail.html",
            signal=None,
            strategy_cls=None,
            regime=None,
        )

    # ---- 2. Regime context at the signal's as_of_date.
    regime: MarketRegime | None = safe(
        lambda: get_regime(signal.as_of_date, session=s),
        label=f"signal_detail[{signal_id}].get_regime",
    )

    # ---- 3. The strategy class — its sizing rule, the
    #         description-and-manual block, and parameter-schema.
    strategy_cls: type[Strategy] | None
    try:
        strategy_cls = STRATEGY_REGISTRY.get(signal.strategy_name)
    except KeyError:
        # Strategy was de-registered or renamed since the signal fired.
        strategy_cls = None

    # ---- 4. Derived sizing breakdown — only meaningful when we have
    #         both a strategy class (for its sizing rule) and a regime row.
    sizing_breakdown: dict[str, object] | None = _sizing_breakdown(
        signal, strategy_cls, regime
    )

    return render(
        request,
        "signals/detail.html",
        signal=signal,
        strategy_cls=strategy_cls,
        regime=regime,
        sizing_breakdown=sizing_breakdown,
    )


def _sizing_breakdown(
    signal: Any,
    strategy_cls: type[Strategy] | None,
    regime: MarketRegime | None,
) -> dict[str, object] | None:
    """Re-derive how the regime layer touched this signal's size.

    The runner sizes each signal by the strategy's own rule (risk against
    the stop, or a fixed fraction of equity) and then, for strategies that
    opt in, multiplies by the day's vol scalar; new longs are refused
    outright while the trend gate is closed or credit stress fires. Only
    the final qty is persisted, so the components are rebuilt here from
    the strategy class and the regime row.

    Returns ``None`` when either input is missing — the template renders
    a "regime data unavailable" note in that case rather than zeros.
    """
    if strategy_cls is None or regime is None:
        return None
    applies = strategy_cls.sizes_down_in_high_vol
    return {
        "regime_label": regime.regime,
        "sizing_rule": (
            f"fixed {strategy_cls.position_pct:.0%} of equity"
            if strategy_cls.position_pct is not None
            else f"risk {strategy_cls.default_risk_pct:.2%} of equity against the stop"
        ),
        "trend_gate_open": regime.trend_gate_open,
        "vol_scalar": regime.vol_multiplier if applies else 1.0,
        "vol_scalar_applies": applies,
        "credit_stress": regime.credit_stress_flag,
        "block_new_longs": regime.block_new_longs and signal.side == "long",
    }
