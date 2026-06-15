"""Paper-trade endpoints — open from a signal, close manually or auto.

Endpoints:
  POST /signals/{signal_id}/paper-trade    — open a paper trade from a signal
  POST /paper-trades/{id}/close            — manually close an open paper trade
  GET  /paper-trades/{id}                  — paper trade detail view
"""

from __future__ import annotations

import json
import logging
from datetime import date
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from stockscan.positions import (
    close_paper_trade,
    get_paper_trade,
    open_paper_trade,
)
from stockscan.regime import get_regime
from stockscan.web.deps import flash_redirect, get_session, render, safe

router = APIRouter()
log = logging.getLogger(__name__)


@router.post("/signals/{signal_id}/paper-trade")
def create_paper_trade(
    signal_id: int,
    request: Request,
    entry_price: str = Form(...),
    stop_price: str = Form(...),
    target_price: str = Form(""),
    qty: int = Form(...),
    s: Session = Depends(get_session),
):
    """Open a paper trade from a signal.

    Pre-fills entry, stop, target, qty from the signal row. Captures a
    snapshot of the signal metadata, technical score, regime context, and
    strategy params at the moment the trade is opened.
    """
    # Validate prices
    try:
        entry = Decimal(entry_price)
        stop = Decimal(stop_price)
        target = Decimal(target_price) if target_price.strip() else None
    except (InvalidOperation, ValueError):
        return flash_redirect(
            f"/signals/{signal_id}", "error", "Invalid price value."
        )
    if qty <= 0:
        return flash_redirect(
            f"/signals/{signal_id}", "error", "Quantity must be positive."
        )

    # Fetch the signal + its context (strategy_configs is retired; we snapshot
    # params from the strategy class itself at trade-open time below).
    sig_sql = text(
        """
        SELECT s.signal_id, s.strategy_name, s.strategy_version,
               s.symbol, s.side, s.as_of_date, s.metadata,
               s.suggested_entry, s.suggested_stop, s.suggested_target,
               s.suggested_qty
        FROM signals s
        WHERE s.signal_id = :sid
        """
    )
    signal = s.execute(sig_sql, {"sid": signal_id}).first()
    if signal is None:
        return flash_redirect("/signals", "error", "Signal not found.")

    # Snapshot the strategy-owned score breakdown at open time. Strategies that
    # compose their score from indicator primitives stash it under
    # metadata.score_breakdown; the parallel technical_scores annotation is gone.
    tech_snapshot = None
    _sb = (signal.metadata or {}).get("score_breakdown") if signal.metadata else None
    if isinstance(_sb, dict) and _sb:
        _sb_meta = _sb.get("_meta", {})
        tech_snapshot = {
            "score": _sb_meta.get("score") if isinstance(_sb_meta, dict) else None,
            "breakdown": _sb,
        }

    # Grab regime snapshot
    regime = safe(
        lambda: get_regime(signal.as_of_date, session=s),
        label="paper_trade.regime",
    )
    regime_snapshot = None
    if regime:
        regime_snapshot = {
            "regime": regime.regime,
            "composite_score": float(regime.composite_score) if regime.composite_score else None,
            "credit_stress_flag": regime.credit_stress_flag,
            "vol_score": float(regime.vol_score) if regime.vol_score else None,
            "trend_score": float(regime.trend_score) if regime.trend_score else None,
            "breadth_score": float(regime.breadth_score) if regime.breadth_score else None,
            "credit_score": float(regime.credit_score) if regime.credit_score else None,
        }

    # Build auto-close rules from signal metadata + the strategy class itself.
    # We snapshot the current-version params at open time (strategy_configs is
    # retired; the file is the source of truth — a version bump is the unit of
    # change). For strategies with no params_model, the knobs are read off the
    # class directly.
    auto_close_rules: dict = {
        "stop_price": float(stop),
    }
    if target:
        auto_close_rules["target_price"] = float(target)

    from stockscan.strategies import STRATEGY_REGISTRY, discover_strategies
    discover_strategies()
    params_json: dict = {}
    try:
        sig_cls = STRATEGY_REGISTRY.get(signal.strategy_name)
        if sig_cls.params_model is not None:
            params_json = sig_cls.params_model().model_dump(mode="json")
        else:
            # Surface the strategy's ClassVar knobs by name. Only attributes the
            # strategy itself declared (not inherited from Strategy) are
            # interesting here.
            params_json = {
                k: getattr(sig_cls, k)
                for k in vars(sig_cls)
                if isinstance(getattr(sig_cls, k), (int, float, str, bool))
                and not k.startswith("_")
                and k not in {"name", "version", "display_name", "description", "manual"}
            }
    except KeyError:
        params_json = {}

    if "holding_days" in (signal.metadata or {}):
        auto_close_rules["time_stop_days"] = signal.metadata["holding_days"]
    elif "max_holding_days" in params_json:
        auto_close_rules["time_stop_days"] = params_json["max_holding_days"]

    paper_trade_id = open_paper_trade(
        signal_id=signal_id,
        strategy_name=signal.strategy_name,
        strategy_version=signal.strategy_version,
        symbol=signal.symbol,
        side=signal.side,
        entry_price=entry,
        stop_price=stop,
        target_price=target,
        qty=qty,
        entry_signal_metadata=signal.metadata,
        entry_tech_score=tech_snapshot,
        entry_regime=regime_snapshot,
        entry_strategy_params=params_json if params_json else None,
        auto_close_rules=auto_close_rules,
        session=s,
    )

    return flash_redirect(
        f"/paper-trades/{paper_trade_id}",
        "success",
        f"Paper trade opened: {signal.symbol} × {qty} @ ${entry:.2f}",
    )


@router.post("/paper-trades/{paper_trade_id}/close")
def close_paper_trade_endpoint(
    paper_trade_id: int,
    request: Request,
    exit_price: str = Form(...),
    exit_reason: str = Form("manual"),
    s: Session = Depends(get_session),
):
    """Manually close an open paper trade."""
    try:
        price = Decimal(exit_price)
    except (InvalidOperation, ValueError):
        return flash_redirect(
            f"/paper-trades/{paper_trade_id}",
            "error",
            "Invalid exit price.",
        )

    pt = get_paper_trade(paper_trade_id, session=s)
    if pt is None:
        return flash_redirect("/trades", "error", "Paper trade not found.")
    if pt.status != "open":
        return flash_redirect(
            f"/paper-trades/{paper_trade_id}",
            "warn",
            "Trade is already closed.",
        )

    # Capture exit-time context
    regime = safe(lambda: get_regime(date.today(), session=s), label="paper_trade_close.regime")
    regime_snapshot = None
    if regime:
        regime_snapshot = {
            "regime": regime.regime,
            "composite_score": float(regime.composite_score) if regime.composite_score else None,
            "credit_stress_flag": regime.credit_stress_flag,
        }

    close_paper_trade(
        paper_trade_id,
        exit_price=price,
        exit_reason=exit_reason,
        exit_regime=regime_snapshot,
        session=s,
    )

    return flash_redirect(
        f"/paper-trades/{paper_trade_id}",
        "success",
        f"Paper trade closed @ ${price:.2f} — {exit_reason}",
    )


@router.get("/paper-trades/{paper_trade_id}")
def paper_trade_detail(
    paper_trade_id: int,
    request: Request,
    s: Session = Depends(get_session),
):
    """Full detail view of a paper trade with entry/exit snapshots."""
    pt = get_paper_trade(paper_trade_id, session=s)
    return render(
        request,
        "paper_trades/detail.html",
        pt=pt,
    )
