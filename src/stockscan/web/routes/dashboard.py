"""Dashboard route — top-level overview."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from stockscan.cycles import compute_calendar_state
from stockscan.db import healthcheck
from stockscan.earnings import earnings_in_window
from stockscan.econ_events import upcoming_events
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
from stockscan.web.routes.refresh import strip_context

router = APIRouter()


@router.get("/")
def dashboard(request: Request, s: Session = Depends(get_session)):
    """Top-level overview — latest equity, the latest scan's passing
    current-version signals, open trades, and the regime card with each strategy's sizing
    rule. The news, calendar, macro, and earnings cards each soft-fail
    independently so one failure never blanks the page."""
    discover_strategies()

    # Latest equity (or fallback)
    eq_row = s.execute(
        text(
            "SELECT total_equity, cash, positions_value, high_water_mark, as_of_date "
            "FROM equity_history ORDER BY as_of_date DESC LIMIT 1"
        )
    ).first()

    # Latest scan: passing signals from the most recent scan date, filtered
    # to the CURRENT registered version of each strategy. Older-version
    # signals stay in the DB for offline comparison but the live dashboard
    # never mixes them with current-version signals.
    version_clause, version_params = current_version_filter(prefix="s")
    passing_row = s.execute(
        text(
            f"""
            SELECT s.as_of_date, count(*) AS n
            FROM signals s
            WHERE s.status = 'new' AND {version_clause}
            GROUP BY s.as_of_date
            ORDER BY s.as_of_date DESC
            LIMIT 1
            """
        ),
        version_params,
    ).first()
    passing_count = passing_row.n if passing_row else 0
    passing_as_of = passing_row.as_of_date if passing_row else None
    sig_rows = []
    if passing_as_of is not None:
        sig_rows = s.execute(
            text(
                f"""
                SELECT s.signal_id, s.strategy_name, s.symbol, s.side, s.score,
                       s.suggested_entry, s.suggested_stop, s.suggested_qty,
                       s.as_of_date
                FROM signals s
                WHERE s.status = 'new'
                  AND s.as_of_date = :d
                  AND {version_clause}
                ORDER BY s.score DESC NULLS LAST, s.symbol
                """
            ),
            {"d": passing_as_of, **version_params},
        ).all()

    open_trades = list_open_trades(session=s)
    health = healthcheck()
    watching = watchlist_symbols(session=s)
    regime = latest_regime(session=s)

    all_strategies = STRATEGY_REGISTRY.all()

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

    # ---- Calendar & Cycles card data ----
    # Soft-fails wholesale: if every indicator inside compute_calendar_state
    # failed AND the orchestrator itself raised, we just hide the card.
    try:
        calendar_state = compute_calendar_state(session=s)
    except Exception as exc:
        _log.warning("dashboard: compute_calendar_state() failed: %s", exc, exc_info=True)
        calendar_state = None

    # ---- Macro this week card data (econ_events) ----
    # Default to "medium" importance min so the dashboard shows real
    # macro risk without burying it under low-amplitude prints. The
    # rolling window is now → 7 days; that's the canonical "what's
    # coming this week" surface for swing-trade entry timing.
    from datetime import datetime as _datetime, timedelta as _timedelta, timezone as _tz
    try:
        now_utc = _datetime.now(_tz.utc)
        macro_events = upcoming_events(
            start=now_utc,
            end=now_utc + _timedelta(days=7),
            country="US",
            importance_min="medium",
            session=s,
        )
    except Exception as exc:
        _log.warning("dashboard: upcoming_events() failed: %s", exc, exc_info=True)
        macro_events = []

    # ---- Earnings this week (watchlist symbols reporting in next 5 days) ----
    try:
        from datetime import date as _date_dash
        today_d = _date_dash.today()
        watch_set = list(watching) if watching else []
        watch_earnings = earnings_in_window(
            watch_set,
            start=today_d,
            end=today_d + _timedelta(days=7),
            session=s,
        )
    except Exception as exc:
        _log.warning("dashboard: earnings_in_window() failed: %s", exc, exc_info=True)
        watch_earnings = []

    return render(
        request,
        "dashboard.html",
        equity=eq_row,
        signals=sig_rows,
        passing_count=passing_count,
        passing_as_of=passing_as_of,
        open_trades=open_trades,
        health=health,
        strategies=all_strategies,
        regime=regime,
        watching=watching,
        news_articles=news_articles,
        news_last_fetched=news_last_fetched,
        calendar_state=calendar_state,
        macro_events=macro_events,
        watch_earnings=watch_earnings,
        **strip_context(),
    )
