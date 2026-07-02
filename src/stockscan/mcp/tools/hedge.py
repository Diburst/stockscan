"""Read-only access to the delta-hedging tool for agents.

These tools let an agent see everything about a running hedge — the option
positions being hedged, the live spot the daemon last saw, the current option
delta / target share count / no-transaction band, the realised + open P&L, the
full stock-fill ledger, and whether the daemon is alive — WITHOUT any ability to
open, close, pause, or otherwise mutate a position. There are deliberately no
write tools here: hedging is steered only from the /hedge web page.

The "what-if" ``at_spot`` argument on :func:`get_hedge` is the key capability —
it re-runs the daemon's exact per-tick decision at a hypothetical price so an
agent can reason about how the hedge will behave as the underlying moves.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from stockscan.hedge import service, store
from stockscan.mcp.serialize import jsonable

# How stale the heartbeat can be before we call the daemon "down".
_HEARTBEAT_ALIVE_SECONDS = 60

_VALID_STATUS = {"active", "paused", "closed"}


def _summary(p: store.HedgePosition) -> dict[str, Any]:
    """Compact one-line view of a hedge position with its live P&L."""
    pnl = service.live_pnl(p)
    return {
        "hedge_position_id": p.hedge_position_id,
        "symbol": p.symbol,
        "option": {
            "side": p.option_side,
            "kind": p.option_kind,
            "strike": float(p.strike),
            "contracts": p.contracts,
            "expiry": jsonable(p.expiry),
        },
        "status": p.status,
        "last_spot": jsonable(p.last_spot),
        "last_tick_at": jsonable(p.last_tick_at),
        "held_shares": p.held_shares,
        "target_shares": p.last_target_shares,
        "option_position_delta": jsonable(p.last_delta),
        "iv_pct": jsonable(p.iv_pct),
        "band_mode": (p.band_policy or {}).get("mode") if p.band_policy else None,
        "pnl": {
            "premium": pnl["premium"],
            "option_open_pnl": pnl["option_open_pnl"],
            "hedge_pnl": pnl["realized_hedge_pnl"] + pnl["hedge_unrealized_pnl"],
            "net_open_pnl": pnl["net_open_pnl"],
        },
    }


def list_hedges(status: str | None = None) -> dict[str, Any]:
    """List delta-hedge positions with their live P&L (read-only).

    Args:
        status: Optional filter — "active", "paused", or "closed". None = all.

    Returns:
        {"count", "status", "hedges": [<summary>, ...]} where each summary
        carries the option leg, last spot the daemon saw, held vs. target
        shares, current delta, and a premium / option / hedge / net P&L block.
    """
    if status is not None and status not in _VALID_STATUS:
        return {"error": "unknown_status", "status": status, "valid": sorted(_VALID_STATUS)}
    positions = store.list_hedge_positions(status=status)
    return {
        "count": len(positions),
        "status": status or "all",
        "hedges": [_summary(p) for p in positions],
    }


def get_hedge(hedge_position_id: int, at_spot: float | None = None) -> dict[str, Any]:
    """Full detail for one hedge position: greeks, band, P&L, recent fills.

    This is the tool to understand *how the hedge is behaving*. Pass ``at_spot``
    to run a what-if: the hedge_state block is recomputed at that hypothetical
    price (delta, target shares, band width, and whether the daemon would trade),
    so you can reason about behavior as the underlying moves — e.g. "as MU nears
    the 1300 strike, what does the hedge do?". Nothing is ever traded.

    Args:
        hedge_position_id: The position id (see list_hedges).
        at_spot: Optional hypothetical spot price for the what-if hedge_state.

    Returns:
        {"position", "pnl", "hedge_state", "recent_adjustments"} or
        {"error": "not_found"}.
    """
    pos = store.get_hedge_position(hedge_position_id)
    if pos is None:
        return {"error": "not_found", "hedge_position_id": hedge_position_id}
    return {
        "position": jsonable(pos),
        "pnl": service.live_pnl(pos),
        "hedge_state": service.hedge_state(pos, spot=at_spot),
        "recent_adjustments": [jsonable(a) for a in store.list_adjustments(hedge_position_id, limit=25)],
    }


def get_hedge_adjustments(hedge_position_id: int, limit: int = 50) -> dict[str, Any]:
    """The stock-fill ledger for a hedge position (most recent first).

    Every buy/sell the daemon made to keep the book delta-neutral, with the
    price, the option delta and target at the time, the running share count, and
    the P&L each fill realised.

    Args:
        hedge_position_id: The position id.
        limit: Max fills to return (default 50, newest first).

    Returns:
        {"hedge_position_id", "count", "adjustments": [...]} or not_found.
    """
    pos = store.get_hedge_position(hedge_position_id)
    if pos is None:
        return {"error": "not_found", "hedge_position_id": hedge_position_id}
    adjustments = store.list_adjustments(hedge_position_id, limit=max(1, min(limit, 500)))
    return {
        "hedge_position_id": hedge_position_id,
        "count": len(adjustments),
        "adjustments": [jsonable(a) for a in adjustments],
    }


def simulate_hedge(
    spot: float | None = None,
    symbol: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    option_side: str = "short",
    option_kind: str = "call",
    strike: float | None = None,
    target_delta: float = 0.30,
    contracts: int = 1,
    dte: int = 30,
    iv_pct: float | None = None,
    rate_pct: float | None = None,
    premium: float | None = None,
    annual_vol_pct: float = 40.0,
    drift_pct: float = 0.0,
    days: float | None = None,
    steps_per_day: int = 39,
    seed: int = 0,
    band_mode: str = "whalley_wilmott",
    risk_aversion: float = 0.05,
    fixed_band_shares: float = 5.0,
    pct_move: float = 0.01,
    cost_rate: float = 0.0005,
    include_series: bool = False,
) -> dict[str, Any]:
    """Simulate the delta hedge over one price path (read-only, no orders).

    Replays the exact live hedging logic offline. Provide ``symbol`` (+ optional
    from_date/to_date) to replay a real stored path, or ``spot`` (+ annual_vol_pct
    / drift_pct / days / seed) for a synthetic GBM path. The option strike is
    solved from ``target_delta`` unless ``strike`` is given; premium defaults to
    Black-Scholes fair value.

    Returns the summary metrics (trades, transaction cost, option/hedge/net P&L,
    tracking error, drawdown) plus the trade list; pass ``include_series=True``
    for the downsampled per-step spot/held/PnL curve.
    """
    from stockscan.hedge import simulate as sim
    from stockscan.hedge.policy import HedgePolicy

    try:
        spec, path = sim.resolve_spec_and_path(
            symbol=symbol, from_date=from_date, to_date=to_date, spot=spot,
            annual_vol_pct=annual_vol_pct, drift_pct=drift_pct, days=days,
            steps_per_day=steps_per_day, seed=seed, option_side=option_side,
            option_kind=option_kind, strike=strike, target_delta=target_delta,
            contracts=contracts, dte=dte, iv_pct=iv_pct, rate_pct=rate_pct, premium=premium,
        )
    except (ValueError, KeyError) as exc:
        return {"error": "bad_scenario", "detail": str(exc)}
    policy = HedgePolicy(mode=band_mode, risk_aversion=risk_aversion,
                         fixed_band_shares=fixed_band_shares, pct_move=pct_move, cost_rate=cost_rate)
    res = sim.simulate_hedge(spec, policy, path, cost_rate=cost_rate)
    out: dict[str, Any] = {"summary": res.summary, "trades": res.trades}
    if include_series:
        out["series"] = res.sampled_series()
    return out


def hedge_monte_carlo(
    spot: float,
    annual_vol_pct: float = 40.0,
    drift_pct: float = 0.0,
    dte: int = 30,
    days: float | None = None,
    n_paths: int = 500,
    steps_per_day: int = 20,
    seed: int = 0,
    option_side: str = "short",
    option_kind: str = "call",
    strike: float | None = None,
    target_delta: float = 0.30,
    contracts: int = 1,
    iv_pct: float | None = None,
    rate_pct: float | None = None,
    premium: float | None = None,
    band_mode: str = "whalley_wilmott",
    risk_aversion: float = 0.05,
    fixed_band_shares: float = 5.0,
    pct_move: float = 0.01,
    cost_rate: float = 0.0005,
) -> dict[str, Any]:
    """Monte Carlo the hedge over many synthetic paths → net-P&L distribution.

    Stress-tests one band setting across luck: returns mean/median/std and the
    5/25/75/95 percentiles of net P&L, win rate, and average trades/cost. Use
    this to judge whether a setting is robust, not just lucky on one path.
    """
    from stockscan.hedge import simulate as sim
    from stockscan.hedge.policy import HedgePolicy

    rate_used = rate_pct if rate_pct is not None else None
    iv_used = iv_pct if iv_pct is not None else annual_vol_pct
    days_used = days if days is not None else float(dte)
    spec = sim.build_option_spec(
        option_kind=option_kind, option_side=option_side, spot=spot, dte=dte,
        iv_pct=iv_used, rate_pct=rate_used if rate_used is not None else _default_rate_pct(),
        contracts=contracts, strike=strike, target_delta=target_delta, premium=premium,
    )
    policy = HedgePolicy(mode=band_mode, risk_aversion=risk_aversion,
                         fixed_band_shares=fixed_band_shares, pct_move=pct_move, cost_rate=cost_rate)
    return sim.monte_carlo(spec, policy, n_paths=n_paths, s0=spot, annual_vol_pct=annual_vol_pct,
                           drift_pct=drift_pct, days=days_used, steps_per_day=steps_per_day,
                           seed=seed, cost_rate=cost_rate)


def hedge_sweep(
    over: str = "risk_aversion",
    values: str = "0.005,0.02,0.05,0.2,1.0",
    spot: float | None = None,
    symbol: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    annual_vol_pct: float = 40.0,
    drift_pct: float = 0.0,
    days: float | None = None,
    steps_per_day: int = 39,
    seed: int = 0,
    mc_paths: int = 0,
    option_side: str = "short",
    option_kind: str = "call",
    strike: float | None = None,
    target_delta: float = 0.30,
    contracts: int = 1,
    dte: int = 30,
    iv_pct: float | None = None,
    rate_pct: float | None = None,
    premium: float | None = None,
    cost_rate: float = 0.0005,
) -> dict[str, Any]:
    """Sweep a band setting across a grid and compare metrics — the tuning tool.

    ``over`` is "risk_aversion" (Whalley-Wilmott) or "fixed_band"; ``values`` is
    a comma-separated grid. Runs on one deterministic path by default, or set
    ``mc_paths>0`` to average each grid point over that many random paths.
    Returns one metrics row per grid value so you can pick the band setting.
    """
    from stockscan.hedge import simulate as sim

    try:
        grid = [float(v) for v in values.split(",") if v.strip()]
    except ValueError:
        return {"error": "bad_values", "detail": "values must be comma-separated numbers"}
    variations = (sim.fixed_band_grid(grid, cost_rate=cost_rate) if over == "fixed_band"
                  else sim.ww_risk_aversion_grid(grid, cost_rate=cost_rate))
    try:
        spec, path = sim.resolve_spec_and_path(
            symbol=symbol, from_date=from_date, to_date=to_date, spot=spot,
            annual_vol_pct=annual_vol_pct, drift_pct=drift_pct, days=days,
            steps_per_day=steps_per_day, seed=seed, option_side=option_side,
            option_kind=option_kind, strike=strike, target_delta=target_delta,
            contracts=contracts, dte=dte, iv_pct=iv_pct, rate_pct=rate_pct, premium=premium,
        )
    except (ValueError, KeyError) as exc:
        return {"error": "bad_scenario", "detail": str(exc)}
    if mc_paths > 0:
        s0 = spot if spot is not None else path.spots[0]
        days_used = days if days is not None else float(dte)
        return sim.sweep(spec, variations, mc={
            "n_paths": mc_paths, "s0": s0, "annual_vol_pct": annual_vol_pct,
            "drift_pct": drift_pct, "days": days_used, "steps_per_day": max(10, steps_per_day // 2),
            "seed": seed,
        })
    return sim.sweep(spec, variations, path=path, cost_rate=cost_rate)


def _default_rate_pct() -> float:
    from stockscan.config import settings

    return settings.risk_free_rate * 100.0


def hedge_daemon_status() -> dict[str, Any]:
    """Is the delta-hedge daemon running, and what is it watching?

    Reads the heartbeat the daemon writes each cycle. ``alive`` is True only if
    the last heartbeat is recent (< 60s) and its status is "running" — a stale
    heartbeat means the daemon is down and positions are NOT being hedged.

    Returns:
        {"present", "alive", "status", "feed_kind", "active_symbols",
         "last_heartbeat_at", "seconds_since_heartbeat", "pid"}.
    """
    hb = store.get_heartbeat()
    if hb is None:
        return {"present": False, "alive": False, "status": "never run"}
    last = hb.get("last_heartbeat_at")
    age = (datetime.now(UTC) - last).total_seconds() if last is not None else None
    alive = age is not None and age < _HEARTBEAT_ALIVE_SECONDS and hb.get("status") == "running"
    return {
        "present": True,
        "alive": alive,
        "status": hb.get("status"),
        "feed_kind": hb.get("feed_kind"),
        "active_symbols": hb.get("active_symbols"),
        "last_heartbeat_at": jsonable(last),
        "seconds_since_heartbeat": round(age, 1) if age is not None else None,
        "pid": hb.get("pid"),
    }
