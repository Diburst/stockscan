"""Paper-trade endpoints — open from a signal, close manually or auto.

Endpoints:
  POST /signals/{signal_id}/paper-trade    — open a paper trade from a signal
  POST /paper-trades/{id}/close            — manually close an open paper trade
  GET  /paper-trades/{id}                  — paper trade detail view
"""

from __future__ import annotations

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
from stockscan.regime import MarketRegime, get_regime
from stockscan.strategies import STRATEGY_REGISTRY, discover_strategies
from stockscan.web.deps import flash_redirect, get_session, render, safe

router = APIRouter()
log = logging.getLogger(__name__)


def _regime_snapshot(regime: MarketRegime | None) -> dict[str, object] | None:
    """The regime controls as a JSON-able dict for the trade's entry/exit
    snapshot columns; None when no row exists for that day."""
    if regime is None:
        return None
    return {
        "regime": regime.regime,
        "trend_gate_open": regime.trend_gate_open,
        "days_on_side": regime.days_on_side,
        "vol_scalar": regime.vol_multiplier,
        "realized_vol_20d": (
            float(regime.realized_vol_20d) if regime.realized_vol_20d is not None else None
        ),
        "credit_stress_flag": regime.credit_stress_flag,
        "hy_oas_level": float(regime.hy_oas_level) if regime.hy_oas_level is not None else None,
    }


@router.post("/signals/{signal_id}/paper-trade")
def create_paper_trade(
    signal_id: int,
    request: Request,
    entry_price: str = Form(...),
    stop_price: str = Form(""),
    target_price: str = Form(""),
    qty: int = Form(...),
    s: Session = Depends(get_session),
):
    """Open a paper trade from a signal.

    Pre-fills entry, stop, target, qty from the signal row. A blank stop is
    allowed — some strategies carry no price stop and exit on rules alone.
    Captures a snapshot of the signal metadata, regime context, and strategy
    knobs at the moment the trade is opened.
    """
    # Validate prices
    try:
        entry = Decimal(entry_price)
        stop = Decimal(stop_price) if stop_price.strip() else None
        target = Decimal(target_price) if target_price.strip() else None
    except (InvalidOperation, ValueError):
        return flash_redirect(
            f"/signals/{signal_id}", "error", "Invalid price value."
        )
    if qty <= 0:
        return flash_redirect(
            f"/signals/{signal_id}", "error", "Quantity must be positive."
        )

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

    regime = safe(
        lambda: get_regime(signal.as_of_date, session=s),
        label="paper_trade.regime",
    )
    regime_snapshot = _regime_snapshot(regime)

    # Auto-close rules from the form plus the strategy's own knobs, which are
    # snapshotted at open time (the file is the source of truth — a version
    # bump is the unit of change).
    auto_close_rules: dict = {}
    if stop is not None:
        auto_close_rules["stop_price"] = float(stop)
    if target:
        auto_close_rules["target_price"] = float(target)

    discover_strategies()
    try:
        params_json = STRATEGY_REGISTRY.get(signal.strategy_name).knobs()
    except KeyError:
        params_json = {}

    if "max_holding_bars" in params_json:
        auto_close_rules["time_stop_days"] = params_json["max_holding_bars"]

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

    regime = safe(lambda: get_regime(date.today(), session=s), label="paper_trade_close.regime")

    close_paper_trade(
        paper_trade_id,
        exit_price=price,
        exit_reason=exit_reason,
        exit_regime=_regime_snapshot(regime),
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
