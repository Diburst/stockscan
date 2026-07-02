"""Higher-level hedge operations shared by the daemon and the web routes.

Keeps the "settle the option + unwind the stock + book the P&L, all in one
transaction" flow in a single place so expiry (daemon) and manual close (web
button) can't drift apart.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from stockscan.hedge import accounting, store

log = logging.getLogger(__name__)


def settle_and_close(
    position: store.HedgePosition,
    *,
    spot: float,
    reason: str,
    session: Session | None = None,
) -> accounting.Settlement:
    """Cash-settle the option at ``spot``, flatten the hedge, and close the row.

    Records the unwind as an ``expiry_settle`` / ``manual_close`` adjustment (if
    there are shares to flatten) and writes the final booked P&L. Returns the
    :class:`accounting.Settlement` for display/logging.
    """
    s = accounting.settle(
        kind=position.option_kind,
        option_side=position.option_side,
        strike=float(position.strike),
        contracts=position.contracts,
        premium=float(position.premium),
        held_shares=position.held_shares,
        avg_cost=float(position.avg_cost),
        realized_hedge_pnl=float(position.realized_hedge_pnl),
        spot=spot,
        multiplier=position.multiplier,
    )

    def _run(sess: Session) -> None:
        if s.unwind_fill_qty != 0:
            store.apply_adjustment(
                position.hedge_position_id,
                fill_qty=s.unwind_fill_qty,
                fill_price=spot,
                spot=spot,
                option_delta=0.0,
                target_shares=0,
                held_before=position.held_shares,
                held_after=0,
                new_avg_cost=0.0,
                new_realized_hedge_pnl=s.realized_hedge_pnl,
                realized_pnl_delta=s.realized_hedge_pnl - float(position.realized_hedge_pnl),
                reason=reason,
                session=sess,
            )
        store.close_hedge_position(
            position.hedge_position_id,
            close_reason=reason,
            settlement_spot=spot,
            realized_pnl=s.total_realized_pnl,
            session=sess,
        )

    if session is not None:
        _run(session)
    else:
        from stockscan.db import session_scope

        with session_scope() as sess:
            _run(sess)

    log.info(
        "hedge #%d settled (%s): ITM=%s intrinsic=%.4f option_pnl=%.2f total_pnl=%.2f",
        position.hedge_position_id, reason, s.in_the_money, s.intrinsic_per_share,
        s.option_settlement_pnl, s.total_realized_pnl,
    )
    return s


def hedge_state(position: store.HedgePosition, *, spot: float | None = None) -> dict[str, object]:
    """What the hedger sees/would-do for ``position`` at ``spot`` (or last spot).

    Read-only: runs the exact same per-tick decision the daemon runs
    (:func:`hedge.daemon.plan_tick`) so an agent can inspect current delta,
    the target share count, the no-transaction band, and whether the daemon
    *would* retrade — including a "what-if" at a hypothetical ``spot`` — without
    ever placing an order.
    """
    from datetime import UTC, datetime

    from stockscan.hedge.daemon import plan_tick  # lazy: avoids import cycle.
    from stockscan.hedge.policy import HedgePolicy

    use_spot = spot if spot is not None else (float(position.last_spot) if position.last_spot else None)
    if use_spot is None or position.iv_pct is None or position.rate_pct is None:
        return {"available": False, "reason": "no spot or volatility yet"}

    policy = HedgePolicy.from_dict(position.band_policy)
    plan = plan_tick(
        held_shares=position.held_shares,
        option_kind=position.option_kind,
        option_side=position.option_side,
        strike=float(position.strike),
        contracts=position.contracts,
        multiplier=position.multiplier,
        iv_pct=float(position.iv_pct),
        rate_pct=float(position.rate_pct),
        expiry=position.expiry,
        policy=policy,
        spot=use_spot,
        last_hedge_spot=float(position.last_hedge_spot) if position.last_hedge_spot else None,
        now=datetime.now(UTC),
    )
    return {
        "available": True,
        "spot": use_spot,
        "is_hypothetical": spot is not None,
        "expired": plan.expired,
        "option_position_delta": round(plan.delta, 2),
        "option_position_gamma": round(plan.gamma, 4),
        "target_shares": plan.target_shares,
        "held_shares": position.held_shares,
        "drift_shares": position.held_shares - plan.target_shares,
        "band_half_width_shares": round(policy.band_shares(use_spot, plan.gamma), 2),
        "band_mode": policy.mode,
        "would_rebalance": plan.rebalance,
        "planned_fill_qty": plan.fill_qty,
    }


def live_pnl(position: store.HedgePosition, *, spot: float | None = None) -> dict[str, float]:
    """Compute the live P&L breakdown for a position at ``spot`` (or last_spot).

    Returns premium, option value / open P&L, hedge realised + unrealised, and
    the net — the numbers the page and CLI status render. Uses a floored time to
    expiry so it's safe right up to the bell.
    """
    from datetime import UTC, datetime

    from stockscan.analysis import black_scholes

    use_spot = spot if spot is not None else (float(position.last_spot) if position.last_spot else None)
    premium = float(position.premium)
    realized_hedge = float(position.realized_hedge_pnl)
    out: dict[str, float] = {
        "premium": premium,
        "realized_hedge_pnl": realized_hedge,
        "hedge_unrealized_pnl": 0.0,
        "option_value": 0.0,
        "option_open_pnl": 0.0,
        "net_open_pnl": realized_hedge,
    }
    if use_spot is None or position.iv_pct is None or position.rate_pct is None:
        return out

    t = black_scholes.years_to_expiry(position.expiry, datetime.now(UTC))
    sigma = float(position.iv_pct) / 100.0
    r = float(position.rate_pct) / 100.0
    value, option_open = accounting.option_mark(
        spot=use_spot,
        strike=float(position.strike),
        t=t,
        r=r,
        sigma=sigma,
        kind=position.option_kind,
        option_side=position.option_side,
        contracts=position.contracts,
        premium=premium,
        multiplier=position.multiplier,
    )
    hedge_unreal = accounting.hedge_unrealized(position.held_shares, float(position.avg_cost), use_spot)
    out.update(
        option_value=value,
        option_open_pnl=option_open,
        hedge_unrealized_pnl=hedge_unreal,
        net_open_pnl=accounting.net_open_pnl(
            option_open_pnl=option_open,
            realized_hedge_pnl=realized_hedge,
            hedge_unrealized_pnl=hedge_unreal,
        ),
    )
    return out
