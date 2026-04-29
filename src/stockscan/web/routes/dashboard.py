"""Dashboard route — top-level overview."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from stockscan.db import healthcheck
from stockscan.positions import list_open_trades
from stockscan.regime import latest_regime
from stockscan.strategies import STRATEGY_REGISTRY, discover_strategies
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

    # Latest signals across all strategies
    sig_rows = s.execute(
        text(
            """
            SELECT signal_id, strategy_name, symbol, side, score, status,
                   suggested_entry, suggested_stop, rejected_reason, as_of_date
            FROM signals
            WHERE as_of_date >= :d
            ORDER BY as_of_date DESC, score DESC NULLS LAST
            LIMIT 25
            """
        ),
        {"d": date.today().replace(day=1)},
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
    )
