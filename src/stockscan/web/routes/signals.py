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

from stockscan.regime import get_regime
from stockscan.regime.store import MarketRegime
from stockscan.scan import signals_freshness
from stockscan.scan.refresh_job import (
    consume_finished as consume_refresh_job,
    current_job as current_refresh_job,
    start_refresh,
)
from stockscan.strategies import (
    STRATEGY_REGISTRY,
    Strategy,
    current_version_filter,
    discover_strategies,
)
from stockscan.web.deps import (
    attach_hx_toast,
    get_session,
    rate_limit_check,
    render,
    safe,
)

router = APIRouter(prefix="/signals")
log = logging.getLogger(__name__)


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


_SORT_COLUMNS: dict[str, str] = {
    "symbol": "s.symbol",
    "strategy": "s.strategy_name",
    "score": "s.score",
    "entry": "s.suggested_entry",
    "stop": "s.suggested_stop",
    "qty": "s.suggested_qty",
    "date": "s.as_of_date",
    "side": "s.side",
}


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

    # Symbol search filter
    if symbol:
        where.append("UPPER(s.symbol) LIKE UPPER(:sym_filter)")
        params["sym_filter"] = f"%{symbol}%"

    # Side filter
    if side and side in ("long", "short"):
        where.append("s.side = :side_filter")
        params["side_filter"] = side

    # Score range filters
    if score_min is not None:
        where.append("s.score >= :score_min")
        params["score_min"] = score_min
    if score_max is not None:
        where.append("s.score <= :score_max")
        params["score_max"] = score_max

    # Restrict to the CURRENT registered version of each strategy.
    version_clause, version_params = current_version_filter(prefix="s")
    where.append(version_clause)
    params.update(version_params)

    # Sorting
    sort_key = sort if sort in _SORT_COLUMNS else None
    direction = "ASC" if sort_dir == "asc" else "DESC"
    if sort_key:
        order_by = f"{_SORT_COLUMNS[sort_key]} {direction} NULLS LAST, s.signal_id DESC"
    else:
        order_by = "s.as_of_date DESC, s.status ASC, s.score DESC NULLS LAST"

    sql = text(
        f"""
        SELECT s.signal_id, s.run_id, s.strategy_name, s.symbol, s.side,
               s.score, s.status, s.as_of_date,
               s.suggested_entry, s.suggested_stop, s.suggested_qty,
               s.rejected_reason, s.metadata
        FROM signals s
        WHERE {" AND ".join(where)}
        ORDER BY {order_by}
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
    via query params. If a background refresh is already in flight, the
    fresh page-load joins its polling loop rather than starting a new job."""
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
    # If a background refresh is in flight (started from another tab or a
    # prior visit), the fresh page-load joins the polling loop too.
    job = current_refresh_job()
    job_running = job is not None and job.status == "running"
    return render(
        request,
        "signals/list.html",
        signals_refresh_error=None,
        signals_refresh_summary=None,
        refresh_job_active=job_running,
        refresh_qs=_refresh_qs(
            strategy=strategy, days=days, show_rejected=show_rejected,
            symbol=symbol, side=side, score_min=score_min, score_max=score_max,
            sort=sort, dir=dir,
        ),
        refresh_elapsed=job.elapsed_seconds if job_running else 0,
        **ctx,
    )


def _refresh_qs(
    *,
    strategy: str | None,
    days: int,
    show_rejected: bool,
    symbol: str | None,
    side: str | None,
    score_min: str | None,
    score_max: str | None,
    sort: str | None,
    dir: str | None,
) -> str:
    """Query string carrying the user's filter/sort state through the
    polling loop, so the post-refresh render matches what they were
    looking at. Only non-empty params are emitted (empty floats 422)."""
    parts = [f"days={days}", f"show_rejected={'true' if show_rejected else 'false'}"]
    for key, value in (
        ("strategy", strategy), ("symbol", symbol), ("side", side),
        ("score_min", score_min), ("score_max", score_max),
        ("sort", sort), ("dir", dir),
    ):
        if value:
            parts.append(f"{key}={value}")
    return "&".join(parts)


@router.post("/refresh")
def refresh_endpoint(
    request: Request,
    strategy: str | None = Query(None),
    days: int = Query(7, ge=1, le=90),
    show_rejected: bool = Query(True),
    # Filters — forwarded so the post-refresh view preserves the user's filters
    symbol: str | None = Query(None),
    side: str | None = Query(None),
    score_min: str | None = Query(None),
    score_max: str | None = Query(None),
    sort: str | None = Query(None),
    dir: str | None = Query(None),
    s: Session = Depends(get_session),
):
    """Start the bars + strategies refresh as a BACKGROUND job.

    Responds immediately with the page-content partial carrying a polling
    strip (``signals/_refresh_status.html`` include); the strip polls
    ``GET /signals/refresh/status`` every 2 s until the job finishes, at
    which point that endpoint swaps in the refreshed content. The page —
    and the rest of the app — stays usable while the refresh runs.

    Single-flight: a second POST while a job is running joins the
    in-flight job instead of starting another. The 20 s cooldown applies
    to job starts.
    """
    _filter_kwargs = dict(
        symbol=symbol, side=side,
        score_min=_to_float(score_min), score_max=_to_float(score_max),
        sort=sort, sort_dir=dir,
    )
    qs = _refresh_qs(
        strategy=strategy, days=days, show_rejected=show_rejected,
        symbol=symbol, side=side, score_min=score_min, score_max=score_max,
        sort=sort, dir=dir,
    )

    def _content(*, job_active: bool, toast: tuple[str, str] | None):
        ctx = _query_signals_view(
            s, strategy=strategy, days=days, show_rejected=show_rejected,
            **_filter_kwargs,
        )
        response = render(
            request,
            "signals/_signals_content.html",
            signals_refresh_error=None,
            signals_refresh_summary=None,
            refresh_job_active=job_active,
            refresh_qs=qs,
            refresh_elapsed=0,
            **ctx,
        )
        if toast:
            return attach_hx_toast(response, *toast)
        return response

    # If a job is already in flight, join it — no new work, no cooldown hit.
    existing = current_refresh_job()
    if existing is not None and existing.status == "running":
        return _content(
            job_active=True,
            toast=("info", "A refresh is already running — joining it"),
        )

    # Signals refresh is the most expensive operation in the app — bars
    # backfill + every strategy re-runs. Debounce job STARTS.
    cooldown_remaining = rate_limit_check("signals.refresh", cooldown_seconds=20)
    if cooldown_remaining is not None:
        return _content(
            job_active=False,
            toast=("warn", f"Just refreshed — try again in {int(cooldown_remaining) + 1}s"),
        )

    _job, started_new = start_refresh(days_back=7)
    toast_msg = (
        "Refresh started — fetching bars and re-running strategies"
        if started_new
        else "A refresh is already running — joining it"
    )
    return _content(job_active=True, toast=("info", toast_msg))


@router.get("/refresh/status")
def refresh_status(
    request: Request,
    strategy: str | None = Query(None),
    days: int = Query(7, ge=1, le=90),
    show_rejected: bool = Query(True),
    symbol: str | None = Query(None),
    side: str | None = Query(None),
    score_min: str | None = Query(None),
    score_max: str | None = Query(None),
    sort: str | None = Query(None),
    dir: str | None = Query(None),
    s: Session = Depends(get_session),
):
    """Polling endpoint for the background refresh.

    While the job runs: returns the small self-replacing status strip
    (it re-polls itself every 2 s — the rest of the page is untouched).

    When the job finishes: returns the FULL refreshed page content with
    ``HX-Retarget: #signals-content`` so this response replaces the whole
    content area, banner + tables, in one swap. The finished job is
    consumed so a stray later poll doesn't re-announce it.
    """
    qs = _refresh_qs(
        strategy=strategy, days=days, show_rejected=show_rejected,
        symbol=symbol, side=side, score_min=score_min, score_max=score_max,
        sort=sort, dir=dir,
    )

    job = current_refresh_job()
    if job is not None and job.status == "running":
        return render(
            request,
            "signals/_refresh_status.html",
            refresh_qs=qs,
            refresh_elapsed=job.elapsed_seconds,
        )

    finished = consume_refresh_job()
    error = finished.error if finished else None
    refresh_summary = finished.summary if finished else None

    ctx = _query_signals_view(
        s, strategy=strategy, days=days, show_rejected=show_rejected,
        symbol=symbol, side=side,
        score_min=_to_float(score_min), score_max=_to_float(score_max),
        sort=sort, sort_dir=dir,
    )
    response = render(
        request,
        "signals/_signals_content.html",
        signals_refresh_error=error,
        signals_refresh_summary=refresh_summary,
        refresh_job_active=False,
        refresh_qs=qs,
        refresh_elapsed=0,
        **ctx,
    )
    # The poll strip is a small element inside #signals-content; retarget
    # the final swap at the whole content block.
    response.headers["HX-Retarget"] = "#signals-content"
    response.headers["HX-Reswap"] = "outerHTML"

    if error:
        return attach_hx_toast(response, "error", "Signal refresh failed")
    if refresh_summary and refresh_summary.get("up_to_date"):
        n_marked = refresh_summary.get("trades_marked", 0)
        n_closed = refresh_summary.get("trades_auto_closed", 0)
        extra = ""
        if n_marked or n_closed:
            extra = f" ({n_marked} marked, {n_closed} auto-closed)"
        return attach_hx_toast(
            response, "info", f"Already up to date — no API calls used{extra}"
        )
    if refresh_summary:
        n_new = refresh_summary.get("signals_emitted", 0)
        parts = [f"{n_new} new signal{'s' if n_new != 1 else ''}"]
        n_marked = refresh_summary.get("trades_marked", 0)
        n_closed = refresh_summary.get("trades_auto_closed", 0)
        if n_marked:
            parts.append(f"{n_marked} trade{'s' if n_marked != 1 else ''} marked")
        if n_closed:
            parts.append(f"{n_closed} auto-closed")
        return attach_hx_toast(
            response, "success", f"Refreshed — {', '.join(parts)}"
        )
    # No job found at all (e.g., server restarted mid-poll) — just rerender.
    return response


@router.get("/{signal_id}")
def signal_detail(
    signal_id: int,
    request: Request,
    s: Session = Depends(get_session),
):
    """Full attribution view: every input that produced this signal's score.

    Pulls together (1) the signal row + JSONB strategy metadata (which
    carries the strategy-owned score breakdown), (2) the regime row at
    the same as_of_date for component-level context, (3) the
    strategy_configs row for the exact params used at scan time, and
    (4) the strategy class itself so we can show the regime-affinity
    table and the human-readable manual.

    Each lookup soft-fails to ``None`` so a missing row in any one
    table never blanks out the whole page.
    """
    discover_strategies()

    # ---- 1. The signal itself, joined with its run metadata.
    sig_sql = text(
        """
        SELECT s.signal_id, s.run_id, s.strategy_name,
               s.strategy_version, s.symbol, s.side, s.score, s.status,
               s.as_of_date, s.suggested_entry, s.suggested_stop,
               s.suggested_target, s.suggested_qty, s.rejected_reason,
               s.metadata,
               r.universe_size, r.signals_emitted, r.rejected_count,
               r.run_at
        FROM signals s
        LEFT JOIN strategy_runs    r ON r.run_id    = s.run_id
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

    # ---- 3. The strategy class — used for affinity lookups, the
    #         description-and-manual block, and parameter-schema.
    strategy_cls: type[Strategy] | None
    try:
        strategy_cls = STRATEGY_REGISTRY.get(signal.strategy_name)
    except KeyError:
        # Strategy was de-registered or renamed since the signal fired.
        strategy_cls = None

    # ---- 4. Derived sizing breakdown — only meaningful when we have
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
