"""Signals page — passing + rejected with badges (USER_STORIES Story 1).

Endpoints:
  GET  /signals                    — full page render
  POST /signals/refresh            — backfill recent bars + re-run all
                                      strategies, then return the
                                      page-content partial for HTMX swap
  GET  /signals/{signal_id}        — detail view
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from stockscan.config import settings
from stockscan.data.providers.eodhd import EODHDError, EODHDProvider
from stockscan.regime import get_regime
from stockscan.regime.store import MarketRegime
from stockscan.scan import refresh_signals, signals_freshness
from stockscan.strategies import (
    STRATEGY_REGISTRY,
    Strategy,
    current_version_filter,
    discover_strategies,
)
from stockscan.web.deps import get_session, render

router = APIRouter(prefix="/signals")
log = logging.getLogger(__name__)


def _query_signals_view(
    s: Session,
    *,
    strategy: str | None,
    days: int,
    show_rejected: bool,
) -> dict[str, Any]:
    """Run the signals SELECT and bundle the template context.

    Extracted from the GET handler so the POST /refresh endpoint can
    re-render the same view without duplicating SQL. Returns the dict
    of kwargs that gets splatted into ``render()``.
    """
    discover_strategies()
    cutoff = date.today() - timedelta(days=days)

    where = ["s.as_of_date >= :d"]
    params: dict[str, object] = {"d": cutoff}
    if strategy:
        where.append("s.strategy_name = :strat")
        params["strat"] = strategy
    if not show_rejected:
        where.append("s.status = 'new'")
    # Restrict to the CURRENT registered version of each strategy.
    # Older-version signals stay in the DB intentionally (use
    # ``stockscan signals delete`` to remove) but never appear in the
    # live signals list. Detail pages remain reachable via direct ID
    # lookup since /signals/{id} doesn't apply this filter.
    version_clause, version_params = current_version_filter(prefix="s")
    where.append(version_clause)
    params.update(version_params)

    sql = text(
        f"""
        SELECT s.signal_id, s.run_id, s.strategy_name, s.symbol, s.side,
               s.score, s.status, s.as_of_date,
               s.suggested_entry, s.suggested_stop, s.suggested_qty,
               s.rejected_reason, s.metadata,
               t.score AS tech_score
        FROM signals s
        LEFT JOIN technical_scores t
          ON t.symbol = s.symbol
         AND t.as_of_date = s.as_of_date
         AND t.strategy_name = s.strategy_name
        WHERE {" AND ".join(where)}
        ORDER BY s.as_of_date DESC, s.status ASC, s.score DESC NULLS LAST
        LIMIT 500
        """
    )
    rows = s.execute(sql, params).all()
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
    }


@router.get("")
async def signals_list(
    request: Request,
    strategy: str | None = Query(None),
    days: int = Query(7, ge=1, le=90),
    show_rejected: bool = Query(True),
    s: Session = Depends(get_session),
):
    ctx = _query_signals_view(
        s, strategy=strategy, days=days, show_rejected=show_rejected
    )
    return render(
        request,
        "signals/list.html",
        signals_refresh_error=None,
        signals_refresh_summary=None,
        **ctx,
    )


@router.post("/refresh")
async def refresh_endpoint(
    request: Request,
    strategy: str | None = Query(None),
    days: int = Query(7, ge=1, le=90),
    show_rejected: bool = Query(True),
    s: Session = Depends(get_session),
):
    """Pull the last 7 days of bars + re-run all strategies, then swap
    the signals page content in place.

    Returns the ``_signals_content.html`` partial — the same body block
    that the GET renders via ``signals/list.html``. HTMX targets
    ``#signals-content`` for ``outerHTML`` replacement so the page
    refreshes without a full reload.

    Filters (``strategy``, ``days``, ``show_rejected``) are forwarded
    so the post-refresh view matches whatever the user was looking at.
    """
    error: str | None = None
    refresh_summary: dict[str, object] | None = None

    api_key = settings.eodhd_api_key.get_secret_value()
    if not api_key:
        error = "EODHD_API_KEY is not set. Add it to your .env to fetch bars."
    else:
        try:
            with EODHDProvider(api_key=api_key) as provider:
                result = refresh_signals(provider, days_back=7, session=s)
            refresh_summary = {
                "bars_upserted": result.bars_upserted,
                "bars_days_covered": result.bars_days_covered,
                "strategies_run": result.strategies_run,
                "signals_emitted": result.signals_emitted,
                "rejected_count": result.rejected_count,
                "failures": [
                    {"strategy_name": f.strategy_name, "error": f.error}
                    for f in result.failures
                ],
                "duration_seconds": round(result.duration_seconds, 1),
            }
        except EODHDError as exc:
            log.warning("signals refresh: provider error: %s", exc)
            error = f"Provider error: {exc}"
        except Exception as exc:
            log.exception("signals refresh: unexpected error")
            error = f"Refresh failed: {exc}"

    ctx = _query_signals_view(
        s, strategy=strategy, days=days, show_rejected=show_rejected
    )
    return render(
        request,
        "signals/_signals_content.html",
        signals_refresh_error=error,
        signals_refresh_summary=refresh_summary,
        **ctx,
    )


@router.get("/{signal_id}")
async def signal_detail(
    signal_id: int,
    request: Request,
    s: Session = Depends(get_session),
):
    """Full attribution view: every input that produced this signal's score.

    Pulls together (1) the signal row + JSONB strategy metadata,
    (2) the regime row at the same as_of_date for component-level
    context, (3) the technical_scores breakdown if computed, (4) the
    strategy_configs row for the exact params used at scan time, and
    (5) the strategy class itself so we can show the regime-affinity
    table and the human-readable manual.

    Each lookup soft-fails to ``None`` so a missing row in any one
    table never blanks out the whole page.
    """
    discover_strategies()

    # ---- 1. The signal itself, joined with its run + config metadata.
    sig_sql = text(
        """
        SELECT s.signal_id, s.run_id, s.config_id, s.strategy_name,
               s.strategy_version, s.symbol, s.side, s.score, s.status,
               s.as_of_date, s.suggested_entry, s.suggested_stop,
               s.suggested_target, s.suggested_qty, s.rejected_reason,
               s.metadata,
               r.universe_size, r.signals_emitted, r.rejected_count,
               r.run_at,
               c.params_json, c.params_hash
        FROM signals s
        LEFT JOIN strategy_runs    r ON r.run_id    = s.run_id
        LEFT JOIN strategy_configs c ON c.config_id = s.config_id
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
            tech_score=None,
        )

    # ---- 2. Regime context at the signal's as_of_date.
    regime: MarketRegime | None
    try:
        regime = get_regime(signal.as_of_date, session=s)
    except Exception as exc:
        log.warning("signal_detail %s: regime lookup failed: %s", signal_id, exc)
        regime = None

    # ---- 3. Technical-confirmation score for (symbol, as_of, strategy).
    tech_sql = text(
        """
        SELECT score, breakdown, computed_at
        FROM technical_scores
        WHERE symbol = :sym AND as_of_date = :d AND strategy_name = :n
        """
    )
    try:
        tech_score = s.execute(
            tech_sql,
            {"sym": signal.symbol, "d": signal.as_of_date, "n": signal.strategy_name},
        ).first()
    except Exception as exc:
        log.warning("signal_detail %s: tech_score lookup failed: %s", signal_id, exc)
        tech_score = None

    # ---- 4. The strategy class — used for affinity lookups, the
    #         description-and-manual block, and parameter-schema.
    strategy_cls: type[Strategy] | None
    try:
        strategy_cls = STRATEGY_REGISTRY.get(signal.strategy_name)
    except KeyError:
        # Strategy was de-registered or renamed since the signal fired.
        strategy_cls = None

    # ---- 5. Derived sizing breakdown — only meaningful when we have
    #         both a strategy class (for affinity) and a regime row.
    sizing_breakdown: dict[str, object] | None = _sizing_breakdown(
        signal, strategy_cls, regime
    )

    return render(
        request,
        "signals/detail.html",
        signal=signal,
        strategy_cls=strategy_cls,
        regime=regime,
        tech_score=tech_score,
        sizing_breakdown=sizing_breakdown,
    )


def _sizing_breakdown(
    signal: Any,
    strategy_cls: type[Strategy] | None,
    regime: MarketRegime | None,
) -> dict[str, object] | None:
    """Re-derive the regime-multiplier components that produced this signal's qty.

    The runner computes ``qty = base_qty x affinity x composite_mult x
    stress_mult`` — but only the final qty is persisted. We can reconstruct
    the multiplier components from the strategy's affinity table and the
    regime row, which is what makes "why did I get THIS many shares?"
    answerable on the detail page.

    Returns ``None`` when either input is missing — the template renders
    a "regime data unavailable" note in that case rather than zeros.
    """
    if strategy_cls is None or regime is None:
        return None

    label = regime.regime
    affinity = float(strategy_cls.affinity_for(label))
    composite_dec = regime.composite_score
    composite = float(composite_dec) if composite_dec is not None else None
    composite_mult = 0.5 + 0.5 * composite if composite is not None else 1.0
    stress_mult = 0.5 if regime.credit_stress_flag else 1.0
    multiplier = affinity * composite_mult * stress_mult
    return {
        "regime_label": label,
        "affinity": affinity,
        "composite": composite,
        "composite_mult": composite_mult,
        "stress_mult": stress_mult,
        "multiplier": multiplier,
        "block_new_longs": bool(regime.credit_stress_flag) and signal.side == "long",
    }
