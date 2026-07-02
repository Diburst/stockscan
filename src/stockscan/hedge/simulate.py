"""Offline hedge simulator — replay the live hedging logic over any price path.

This is the tuning playground's engine. It reuses the daemon's exact per-tick
decision (:func:`hedge.daemon.plan_tick`) and the same pure fill/settlement
accounting, so what you measure here is precisely what the live daemon would do
— just fast, deterministic (seeded), and offline (no DB, no websocket, no
wall-clock).

Four capabilities, built from one engine:
  * :func:`gbm_path`        — a synthetic geometric-Brownian-motion price path.
  * :func:`historical_path` — a real symbol's path from stored daily bars, with
                              an intraday shape routed through each bar's OHLC so
                              the no-transaction band actually gets exercised
                              (daily closes alone would never trigger it).
  * :func:`simulate_hedge`  — run one path, return trades + P&L series + metrics.
  * :func:`monte_carlo`     — many synthetic paths → distribution of outcomes.
  * :func:`sweep`           — one path (or MC) across a grid of band settings.

Transaction cost is *modeled* here (``cost_rate``) even though the paper daemon
fills at spot for free — because the whole point of tuning the band is the
tracking-error-vs-cost tradeoff, which is invisible without a cost.
"""

from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from stockscan.analysis import black_scholes
from stockscan.hedge import accounting
from stockscan.hedge.daemon import plan_tick
from stockscan.hedge.policy import WHALLEY_WILMOTT, HedgePolicy

_TRADING_DAY_OPEN = time(13, 30)  # ~09:30 ET in UTC (approx; playground only).
_TRADING_DAY_CLOSE = time(20, 0)  # ~16:00 ET in UTC.


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class PricePath:
    times: list[datetime]
    spots: list[float]
    label: str
    source: str  # 'synthetic' | 'historical'
    symbol: str | None = None
    steps_per_day: int = 1

    def __len__(self) -> int:
        return len(self.spots)


@dataclass(frozen=True, slots=True)
class OptionSpec:
    option_kind: str
    option_side: str
    strike: float
    contracts: int
    premium: float
    iv_pct: float
    rate_pct: float
    expiry: datetime
    multiplier: int = 100


@dataclass(slots=True)
class HedgeSimResult:
    summary: dict[str, Any]
    series: list[dict[str, Any]] = field(default_factory=list)
    trades: list[dict[str, Any]] = field(default_factory=list)

    def sampled_series(self, max_points: int = 240) -> list[dict[str, Any]]:
        """Downsample the per-step series for charting (keeps first + last)."""
        n = len(self.series)
        if n <= max_points:
            return self.series
        step = n / max_points
        idx = sorted({int(i * step) for i in range(max_points)} | {0, n - 1})
        return [self.series[i] for i in idx]


# ---------------------------------------------------------------------------
# Price paths
# ---------------------------------------------------------------------------
def gbm_path(
    *,
    s0: float,
    annual_vol_pct: float,
    drift_pct: float = 0.0,
    days: float,
    steps_per_day: int = 78,
    seed: int = 0,
    start: datetime | None = None,
) -> PricePath:
    """A seeded geometric-Brownian-motion path over ``days`` calendar days.

    ``annual_vol_pct`` / ``drift_pct`` are in percent (30 = 30% vol). Time is
    annualised on a 365-day basis to match Black-Scholes ``t``, so realized vol
    over the path ≈ the input vol.
    """
    start = start or datetime.now(UTC)
    n = max(1, int(round(days * steps_per_day)))
    dt = (days / 365.0) / n
    sd = (annual_vol_pct / 100.0) * math.sqrt(dt)
    mu = (drift_pct / 100.0 - 0.5 * (annual_vol_pct / 100.0) ** 2) * dt
    rng = random.Random(seed)
    total_seconds = days * 86400.0
    times: list[datetime] = []
    spots: list[float] = []
    s = s0
    for i in range(n + 1):
        times.append(start + timedelta(seconds=total_seconds * i / n))
        spots.append(round(s, 4))
        s = max(0.01, s * math.exp(mu + sd * rng.gauss(0.0, 1.0)))
    return PricePath(times, spots, label=f"GBM σ={annual_vol_pct:g}% {days:g}d",
                     source="synthetic", steps_per_day=steps_per_day)


def _intraday_points(o: float, h: float, low: float, c: float, steps: int) -> list[float]:
    """Plausible within-day path visiting the true high and low, ending at close.

    Up days dip to the low then rally to the high; down days pop to the high then
    slide to the low. Endpoints are the real open/close, so concatenating bars
    reproduces overnight gaps too.
    """
    anchors = [o, low, h, c] if c >= o else [o, h, low, c]
    seg = max(1, steps // (len(anchors) - 1))
    pts: list[float] = []
    for a, b in zip(anchors[:-1], anchors[1:]):
        for k in range(seg):
            f = k / seg
            pts.append(round(a + (b - a) * f, 4))
    pts.append(round(c, 4))
    return pts


def historical_path(
    symbol: str,
    start: date,
    end: date,
    *,
    steps_per_day: int = 12,
    session: Any | None = None,
) -> PricePath:
    """Replay a real symbol from stored daily bars, with an intraday OHLC shape.

    Raises ``ValueError`` if there aren't enough bars. Uses adjusted OHLC.
    """
    from stockscan.data.store import get_bars

    bars = get_bars(symbol, start, end, session=session)
    if bars is None or bars.empty or len(bars) < 2:
        raise ValueError(f"not enough bars for {symbol} in {start}..{end}")

    times: list[datetime] = []
    spots: list[float] = []
    open_t, close_t = _TRADING_DAY_OPEN, _TRADING_DAY_CLOSE
    span = (datetime.combine(date.today(), close_t) - datetime.combine(date.today(), open_t)).total_seconds()
    for ts, row in bars.iterrows():
        day = ts.date() if hasattr(ts, "date") else ts
        o, h, low, c = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])
        pts = _intraday_points(o, h, low, c, steps_per_day)
        day_open = datetime.combine(day, open_t, tzinfo=UTC)
        for j, price in enumerate(pts):
            times.append(day_open + timedelta(seconds=span * j / max(1, len(pts) - 1)))
            spots.append(price)
    return PricePath(times, spots, label=f"{symbol} {start}..{end}",
                     source="historical", symbol=symbol, steps_per_day=steps_per_day)


# ---------------------------------------------------------------------------
# Building an option spec (auto strike / premium)
# ---------------------------------------------------------------------------
def build_option_spec(
    *,
    option_kind: str,
    option_side: str,
    spot: float,
    dte: int,
    iv_pct: float,
    rate_pct: float,
    contracts: int = 1,
    strike: float | None = None,
    target_delta: float | None = None,
    premium: float | None = None,
    multiplier: int = 100,
    start: datetime | None = None,
) -> OptionSpec:
    """Assemble an OptionSpec, solving strike-from-delta and BS premium if absent."""
    start = start or datetime.now(UTC)
    expiry = start + timedelta(days=dte)
    t = max(dte, 1) / 365.0
    sigma = iv_pct / 100.0
    r = rate_pct / 100.0
    if strike is None:
        delta = target_delta if target_delta is not None else 0.30
        strike = black_scholes.strike_for_delta(
            spot, t, r, sigma, delta if option_kind == "call" else -delta, option_kind
        )
    if premium is None:
        premium = black_scholes.price(spot, strike, t, r, sigma, option_kind) * multiplier * contracts
    return OptionSpec(
        option_kind=option_kind, option_side=option_side, strike=round(strike, 4),
        contracts=contracts, premium=round(premium, 2), iv_pct=iv_pct, rate_pct=rate_pct,
        expiry=expiry, multiplier=multiplier,
    )


def resolve_spec_and_path(
    *,
    symbol: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    spot: float | None = None,
    annual_vol_pct: float = 40.0,
    drift_pct: float = 0.0,
    days: float | None = None,
    steps_per_day: int = 39,
    seed: int = 0,
    option_side: str = "short",
    option_kind: str = "call",
    strike: float | None = None,
    target_delta: float = 0.30,
    contracts: int = 1,
    dte: int = 30,
    iv_pct: float | None = None,
    rate_pct: float | None = None,
    premium: float | None = None,
    session: Any | None = None,
) -> tuple[OptionSpec, PricePath]:
    """Build (OptionSpec, PricePath) from flat scenario params — the one resolver
    shared by the CLI, the MCP tools, and the web playground.

    ``symbol`` selects the historical source (bars → intraday path, IV from
    realized vol, option expires at the window end); otherwise a synthetic GBM
    path is built from ``spot`` / ``annual_vol_pct`` / ``drift_pct``.
    """
    from datetime import date, timedelta

    from stockscan.config import settings

    rate_used = rate_pct if rate_pct is not None else settings.risk_free_rate * 100.0
    if symbol:
        from stockscan.hedge import vol as hvol

        end = date.fromisoformat(to_date) if to_date else date.today()
        start = date.fromisoformat(from_date) if from_date else end - timedelta(days=90)
        path = historical_path(symbol, start, end, steps_per_day=steps_per_day, session=session)
        s0 = path.spots[0]
        iv_used = iv_pct if iv_pct is not None else (hvol.realized_vol_pct(symbol, session=session) or annual_vol_pct)
        dte_used = max(1, round((path.times[-1] - path.times[0]).total_seconds() / 86400))
    else:
        if spot is None:
            raise ValueError("provide symbol (historical) or spot (synthetic)")
        s0 = spot
        iv_used = iv_pct if iv_pct is not None else annual_vol_pct
        dte_used = dte
        days_used = days if days is not None else float(dte)
        path = gbm_path(s0=s0, annual_vol_pct=annual_vol_pct, drift_pct=drift_pct,
                        days=days_used, steps_per_day=steps_per_day, seed=seed)
    spec = build_option_spec(
        option_kind=option_kind, option_side=option_side, spot=s0, dte=dte_used,
        iv_pct=iv_used, rate_pct=rate_used, contracts=contracts, strike=strike,
        target_delta=target_delta, premium=premium, start=path.times[0],
    )
    return spec, path


# ---------------------------------------------------------------------------
# The single-path engine
# ---------------------------------------------------------------------------
def simulate_hedge(
    spec: OptionSpec,
    policy: HedgePolicy,
    path: PricePath,
    *,
    cost_rate: float | None = None,
) -> HedgeSimResult:
    """Run one price path through the live hedging logic; return trades + metrics."""
    cost = policy.cost_rate if cost_rate is None else cost_rate
    sigma = spec.iv_pct / 100.0
    r = spec.rate_pct / 100.0

    held = 0
    avg_cost = 0.0
    realized = 0.0
    last_hedge_spot: float | None = None
    txn_cost = 0.0
    trades: list[dict[str, Any]] = []
    series: list[dict[str, Any]] = []
    settled: accounting.Settlement | None = None
    settle_spot = path.spots[-1]

    for t, spot in zip(path.times, path.spots):
        plan = plan_tick(
            held_shares=held, option_kind=spec.option_kind, option_side=spec.option_side,
            strike=spec.strike, contracts=spec.contracts, multiplier=spec.multiplier,
            iv_pct=spec.iv_pct, rate_pct=spec.rate_pct, expiry=spec.expiry,
            policy=policy, spot=spot, last_hedge_spot=last_hedge_spot, now=t,
        )
        if plan.expired:
            settle_spot = spot
            break
        if plan.rebalance and plan.fill_qty != 0:
            fr = accounting.apply_fill(
                held_shares=held, avg_cost=avg_cost, realized_pnl=realized,
                fill_qty=plan.fill_qty, fill_price=spot,
            )
            txn_cost += abs(plan.fill_qty) * spot * cost
            held, avg_cost, realized = fr.held_shares, fr.avg_cost, fr.realized_pnl
            last_hedge_spot = spot
            trades.append({
                "ts": t.isoformat(), "side": "buy" if plan.fill_qty > 0 else "sell",
                "qty": abs(plan.fill_qty), "price": round(spot, 4),
                "delta": round(plan.delta, 2), "target": plan.target_shares,
                "held_after": held, "realized_delta": round(fr.realized_delta, 2),
            })

        t_years = black_scholes.years_to_expiry(spec.expiry, t)
        value, opt_open = accounting.option_mark(
            spot=spot, strike=spec.strike, t=t_years, r=r, sigma=sigma,
            kind=spec.option_kind, option_side=spec.option_side,
            contracts=spec.contracts, premium=spec.premium, multiplier=spec.multiplier,
        )
        hedge_unreal = accounting.hedge_unrealized(held, avg_cost, spot)
        net = opt_open + realized + hedge_unreal - txn_cost
        series.append({
            "ts": t.isoformat(), "spot": round(spot, 4), "held": held,
            "target": plan.target_shares, "delta": round(plan.delta, 2),
            "option_pnl": round(opt_open, 2), "hedge_pnl": round(realized + hedge_unreal, 2),
            "txn_cost": round(txn_cost, 2), "net_pnl": round(net, 2),
            "residual": held - plan.target_shares,
        })

    # Terminal settlement (at expiry if we broke early, else at the last spot).
    settled = accounting.settle(
        kind=spec.option_kind, option_side=spec.option_side, strike=spec.strike,
        contracts=spec.contracts, premium=spec.premium, held_shares=held,
        avg_cost=avg_cost, realized_hedge_pnl=realized, spot=settle_spot,
        multiplier=spec.multiplier,
    )
    txn_cost += abs(settled.unwind_fill_qty) * settle_spot * cost
    shares_traded = sum(t["qty"] for t in trades) + abs(settled.unwind_fill_qty)

    option_pnl = settled.option_settlement_pnl
    hedge_pnl = settled.realized_hedge_pnl
    gross = option_pnl + hedge_pnl
    net = gross - txn_cost

    residuals = [s["residual"] for s in series] or [0]
    net_curve = [s["net_pnl"] for s in series] or [0.0]
    running_max = net_curve[0]
    max_dd = 0.0
    for v in net_curve:
        running_max = max(running_max, v)
        max_dd = min(max_dd, v - running_max)

    summary = {
        "label": path.label,
        "source": path.source,
        "symbol": path.symbol,
        "option": f"{spec.option_side} {spec.contracts}x {spec.option_kind} {spec.strike:g}",
        "premium": round(spec.premium, 2),
        "iv_pct": spec.iv_pct,
        "band_mode": policy.mode,
        "risk_aversion": policy.risk_aversion,
        "cost_rate": cost,
        "steps": len(path),
        "num_trades": len(trades),
        "shares_traded": shares_traded,
        "transaction_cost": round(txn_cost, 2),
        "option_pnl": round(option_pnl, 2),
        "hedge_pnl": round(hedge_pnl, 2),
        "gross_pnl": round(gross, 2),
        "net_pnl": round(net, 2),
        "net_pct_premium": round(net / spec.premium * 100.0, 1) if spec.premium else None,
        "max_drawdown": round(max_dd, 2),
        "avg_abs_residual": round(statistics.fmean(abs(x) for x in residuals), 2),
        "tracking_error_shares": round(statistics.pstdev(residuals) if len(residuals) > 1 else 0.0, 2),
        "in_the_money": settled.in_the_money,
    }
    return HedgeSimResult(summary=summary, series=series, trades=trades)


# ---------------------------------------------------------------------------
# Monte Carlo
# ---------------------------------------------------------------------------
def monte_carlo(
    spec: OptionSpec,
    policy: HedgePolicy,
    *,
    n_paths: int,
    s0: float,
    annual_vol_pct: float,
    drift_pct: float = 0.0,
    days: float,
    steps_per_day: int = 39,
    seed: int = 0,
    cost_rate: float | None = None,
) -> dict[str, Any]:
    """Run ``n_paths`` seeded GBM paths; return the distribution of outcomes."""
    nets: list[float] = []
    trades: list[int] = []
    costs: list[float] = []
    option_pnls: list[float] = []
    hedge_pnls: list[float] = []
    for i in range(max(1, n_paths)):
        path = gbm_path(s0=s0, annual_vol_pct=annual_vol_pct, drift_pct=drift_pct,
                        days=days, steps_per_day=steps_per_day, seed=seed + i, start=spec.expiry - timedelta(days=days))
        res = simulate_hedge(spec, policy, path, cost_rate=cost_rate)
        nets.append(res.summary["net_pnl"])
        trades.append(res.summary["num_trades"])
        costs.append(res.summary["transaction_cost"])
        option_pnls.append(res.summary["option_pnl"])
        hedge_pnls.append(res.summary["hedge_pnl"])

    def _pct(xs: list[float], q: float) -> float:
        xs2 = sorted(xs)
        idx = min(len(xs2) - 1, max(0, int(round(q * (len(xs2) - 1)))))
        return round(xs2[idx], 2)

    return {
        "n_paths": len(nets),
        "band_mode": policy.mode,
        "risk_aversion": policy.risk_aversion,
        "net_pnl": {
            "mean": round(statistics.fmean(nets), 2),
            "median": round(statistics.median(nets), 2),
            "std": round(statistics.pstdev(nets) if len(nets) > 1 else 0.0, 2),
            "min": round(min(nets), 2), "max": round(max(nets), 2),
            "p5": _pct(nets, 0.05), "p25": _pct(nets, 0.25),
            "p75": _pct(nets, 0.75), "p95": _pct(nets, 0.95),
        },
        "win_rate": round(sum(1 for x in nets if x > 0) / len(nets), 3),
        "avg_trades": round(statistics.fmean(trades), 1),
        "avg_cost": round(statistics.fmean(costs), 2),
        "avg_option_pnl": round(statistics.fmean(option_pnls), 2),
        "avg_hedge_pnl": round(statistics.fmean(hedge_pnls), 2),
        "samples": [round(x, 2) for x in nets],
    }


# ---------------------------------------------------------------------------
# Parameter sweep
# ---------------------------------------------------------------------------
def ww_risk_aversion_grid(values: list[float], *, cost_rate: float = 0.0005) -> list[tuple[str, HedgePolicy]]:
    """Whalley-Wilmott policies across a range of risk-aversion values."""
    return [(f"a={v:g}", HedgePolicy(mode=WHALLEY_WILMOTT, risk_aversion=v, cost_rate=cost_rate)) for v in values]


def fixed_band_grid(values: list[float], *, cost_rate: float = 0.0005) -> list[tuple[str, HedgePolicy]]:
    """Fixed-share-band policies across a range of band widths."""
    from stockscan.hedge.policy import FIXED_SHARES

    return [(f"band={v:g}", HedgePolicy(mode=FIXED_SHARES, fixed_band_shares=v, cost_rate=cost_rate)) for v in values]


def sweep(
    spec: OptionSpec,
    variations: list[tuple[str, HedgePolicy]],
    *,
    path: PricePath | None = None,
    mc: dict[str, Any] | None = None,
    cost_rate: float | None = None,
) -> dict[str, Any]:
    """Run each (label, policy) variation and tabulate the metrics for comparison.

    Provide either ``path`` (one deterministic path — fast, apples-to-apples) or
    ``mc`` (a dict of monte_carlo kwargs, minus spec/policy — robustness across
    many paths). Exactly one should be given.
    """
    if (path is None) == (mc is None):
        raise ValueError("provide exactly one of path= or mc=")
    rows: list[dict[str, Any]] = []
    for label, policy in variations:
        if path is not None:
            r = simulate_hedge(spec, policy, path, cost_rate=cost_rate)
            s = r.summary
            rows.append({
                "variation": label, "band_mode": policy.mode, "risk_aversion": policy.risk_aversion,
                "fixed_band_shares": policy.fixed_band_shares,
                "num_trades": s["num_trades"], "transaction_cost": s["transaction_cost"],
                "tracking_error_shares": s["tracking_error_shares"],
                "net_pnl": s["net_pnl"], "net_pct_premium": s["net_pct_premium"],
            })
        else:
            m = monte_carlo(spec, policy, cost_rate=cost_rate, **mc)
            rows.append({
                "variation": label, "band_mode": policy.mode, "risk_aversion": policy.risk_aversion,
                "avg_trades": m["avg_trades"], "avg_cost": m["avg_cost"],
                "net_pnl_mean": m["net_pnl"]["mean"], "net_pnl_median": m["net_pnl"]["median"],
                "net_pnl_p5": m["net_pnl"]["p5"], "win_rate": m["win_rate"],
            })
    return {
        "mode": "single_path" if path is not None else "monte_carlo",
        "path_label": path.label if path is not None else None,
        "count": len(rows),
        "rows": rows,
    }
