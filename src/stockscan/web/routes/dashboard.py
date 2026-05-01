"""Dashboard route — top-level overview."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from stockscan.db import healthcheck
from stockscan.news import last_fetched_at as news_last_fetched_at
from stockscan.news import recent_general as recent_news
from stockscan.positions import list_open_trades
from stockscan.regime import latest_regime
from stockscan.strategies import (
    STRATEGY_REGISTRY,
    current_version_filter,
    discover_strategies,
)
from stockscan.watchlist import watchlist_symbols
from stockscan.web.deps import get_session, render

router = APIRouter()


@router.get("/")
async def dashboard(request: Request, s: Session = Depends(get_session)):
    discover_strategies()

    # Latest equity (or fallback)
    eq_row = s.execute(
        text(
            "SELECT total_equity, cash, positions_value, high_water_mark, as_of_date "
            "FROM equity_history ORDER BY as_of_date DESC LIMIT 1"
        )
    ).first()

    # Latest signals across all strategies — filtered to the CURRENT
    # registered version of each strategy. Older-version signals stay
    # in the DB for offline comparison but the live dashboard never
    # mixes them with current-version signals.
    version_clause, version_params = current_version_filter(prefix="s")
    sig_rows = s.execute(
        text(
            f"""
            SELECT s.signal_id, s.strategy_name, s.symbol, s.side, s.score,
                   s.status, s.suggested_entry, s.suggested_stop,
                   s.rejected_reason, s.as_of_date
            FROM signals s
            WHERE s.as_of_date >= :d
              AND {version_clause}
            ORDER BY s.as_of_date DESC, s.score DESC NULLS LAST
            LIMIT 25
            """
        ),
        {"d": date.today().replace(day=1), **version_params},
    ).all()

    open_trades = list_open_trades(session=s)
    health = healthcheck()
    watching = watchlist_symbols(session=s)
    regime = latest_regime(session=s)

    all_strategies = STRATEGY_REGISTRY.all()

    # ---- v2 soft sizing: per-strategy multiplier breakdown for the banner.
    # Each entry has: cls, affinity, composite_mult, stress_mult, effective.
    # active/inactive are kept (computed from the effective multiplier) for
    # back-compat with anything still keying off those names.
    strategy_factors: list[dict[str, object]] = []
    if regime is not None:
        composite_dec = regime.composite_score
        composite = float(composite_dec) if composite_dec is not None else None
        composite_mult = 0.5 + 0.5 * composite if composite is not None else 1.0
        stress_mult = 0.5 if regime.credit_stress_flag else 1.0
        for cls in all_strategies:
            affinity = cls.affinity_for(regime.regime)
            effective = affinity * composite_mult * stress_mult
            strategy_factors.append(
                {
                    "cls": cls,
                    "affinity": affinity,
                    "composite_mult": composite_mult,
                    "stress_mult": stress_mult,
                    "effective": effective,
                }
            )
        active_strategies = [sf["cls"] for sf in strategy_factors if (sf["effective"] or 0.0) > 0]
        inactive_strategies = [
            sf["cls"] for sf in strategy_factors if (sf["effective"] or 0.0) == 0
        ]
    else:
        for cls in all_strategies:
            strategy_factors.append(
                {
                    "cls": cls,
                    "affinity": cls.default_affinity,
                    "composite_mult": 1.0,
                    "stress_mult": 1.0,
                    "effective": 1.0,
                }
            )
        active_strategies = all_strategies
        inactive_strategies = []

    # ---- News card data ----
    # Soft-fail per call (independent try/except per fetch) so an issue
    # with one query doesn't blank out the whole card. Log the actual
    # exception at warning level — the previous bare-except was swallowing
    # errors silently and made debugging painful.
    import logging

    _log = logging.getLogger(__name__)
    try:
        news_articles = recent_news(limit=10, session=s)
    except Exception as exc:
        _log.warning("dashboard: recent_news() failed: %s", exc, exc_info=True)
        news_articles = []
    try:
        news_last_fetched = news_last_fetched_at(session=s)
    except Exception as exc:
        _log.warning("dashboard: news_last_fetched_at() failed: %s", exc, exc_info=True)
        news_last_fetched = None

    return render(
        request,
        "dashboard.html",
        equity=eq_row,
        signals=sig_rows,
        open_trades=open_trades,
        health=health,
        strategies=all_strategies,
        active_strategies=active_strategies,
        inactive_strategies=inactive_strategies,
        strategy_factors=strategy_factors,
        regime=regime,
        watching=watching,
        news_articles=news_articles,
        news_last_fetched=news_last_fetched,
        news_refresh_error=None,
        news_refresh_summary=None,
    )
