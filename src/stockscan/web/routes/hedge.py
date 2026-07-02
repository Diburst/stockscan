"""Delta-hedging web endpoints — create a fake option, enable hedging, track it.

  GET  /hedge                     — list positions (live P&L) + create form + daemon status
  GET  /hedge/suggest             — HTMX: recompute strike/premium/expiry for a symbol
  POST /hedge                     — create a fake option position and enable hedging
  POST /hedge/{id}/toggle         — pause / resume hedging
  POST /hedge/{id}/close          — manually settle + close
  GET  /hedge/{id}                — detail: P&L breakdown + adjustment ledger

The daemon (`stockscan hedge run`) does the real-time hedging; these routes only
create/steer positions and read the state the daemon writes. The page polls
itself via HTMX to stay live (consistent with the rest of the app).

All user input is sanitized server-side (see `_parse_decimal` / `_sanitize_symbol`)
so malformed values — stray commas, `$`, letters, out-of-range numbers — are
rejected with a flash rather than reaching the DB or a formatter.
"""

from __future__ import annotations

import calendar
import logging
import re
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.orm import Session

from stockscan.analysis import black_scholes
from stockscan.config import settings
from stockscan.data.store import get_bars
from stockscan.hedge import service, store, vol
from stockscan.hedge.policy import FIXED_SHARES, PCT_MOVE, WHALLEY_WILMOTT, HedgePolicy
from stockscan.watchlist.store import list_watchlist
from stockscan.web.deps import flash_redirect, get_session, render

router = APIRouter()
log = logging.getLogger(__name__)

_VALID_MODES = {WHALLEY_WILMOTT, FIXED_SHARES, PCT_MOVE}
_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
_MAX_CONTRACTS = 100_000
_SUGGEST_DELTA = 0.30  # default short strike ~30-delta OTM.


class _FormError(ValueError):
    """User-facing validation error; message is safe to show in a flash."""


# ---------------------------------------------------------------------------
# Input sanitization
# ---------------------------------------------------------------------------
def _sanitize_symbol(raw: str) -> str:
    sym = (raw or "").strip().upper()
    if not _SYMBOL_RE.match(sym):
        raise _FormError("Symbol must be 1–10 letters/digits (e.g. MU, BRK.B).")
    return sym


def _parse_decimal(raw: str, *, field: str, min_value: float | None = None,
                   max_value: float | None = None, allow_zero: bool = False) -> Decimal:
    """Parse a money/number field, tolerating `$`, commas, and spaces."""
    txt = (raw or "").replace(",", "").replace("$", "").strip()
    if not txt:
        raise _FormError(f"{field} is required.")
    try:
        val = Decimal(txt)
    except (InvalidOperation, ValueError):
        raise _FormError(f"{field} must be a number.") from None
    if not val.is_finite():
        raise _FormError(f"{field} must be a finite number.")
    if not allow_zero and val <= 0:
        raise _FormError(f"{field} must be greater than zero.")
    if allow_zero and val < 0:
        raise _FormError(f"{field} cannot be negative.")
    if min_value is not None and val < Decimal(str(min_value)):
        raise _FormError(f"{field} must be at least {min_value:g}.")
    if max_value is not None and val > Decimal(str(max_value)):
        raise _FormError(f"{field} must be at most {max_value:g}.")
    return val


def _parse_float(raw: str, *, field: str, lo: float, hi: float) -> float:
    return float(_parse_decimal(raw, field=field, min_value=lo, max_value=hi))


def _parse_signed_float(raw: str, *, field: str, lo: float, hi: float) -> float:
    """Parse a possibly-negative number (e.g. drift), tolerating commas/spaces."""
    txt = (raw or "").replace(",", "").strip()
    if not txt:
        return 0.0
    try:
        val = float(txt)
    except ValueError:
        raise _FormError(f"{field} must be a number.") from None
    if not (lo <= val <= hi):
        raise _FormError(f"{field} must be between {lo:g} and {hi:g}.")
    return val


# ---------------------------------------------------------------------------
# Real defaults for the create form
# ---------------------------------------------------------------------------
def _third_friday(year: int, month: int) -> date:
    weeks = calendar.monthcalendar(year, month)
    fridays = [w[calendar.FRIDAY] for w in weeks if w[calendar.FRIDAY]]
    return date(year, month, fridays[2])


def _next_monthly_opex(today: date) -> date:
    """Next standard monthly options expiry (3rd Friday), ≥ ~1 week out."""
    opex = _third_friday(today.year, today.month)
    if (opex - today).days < 7:
        year, month = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
        opex = _third_friday(year, month)
    return opex


def _last_close(symbol: str, session: Session) -> float | None:
    try:
        end = datetime.now(UTC)
        bars = get_bars(symbol, end - timedelta(days=15), end, session=session)
        if bars is not None and not bars.empty:
            return float(bars["close"].iloc[-1])
    except Exception as exc:  # noqa: BLE001 - best-effort default
        log.debug("hedge: last_close lookup failed for %s: %s", symbol, exc)
    return None


def _round_strike(x: float) -> float:
    inc = 5.0 if x >= 100 else (1.0 if x >= 25 else 0.5)
    return round(x / inc) * inc


def _suggest_trade(symbol: str, kind: str, expiry: date, session: Session) -> dict[str, object]:
    """Suggest a realistic strike + premium for the symbol at ~30-delta.

    Returns blank strike/premium (but keeps iv/spot when available) if there
    aren't enough bars to price it, so the form still renders cleanly.
    """
    out: dict[str, object] = {"strike": "", "premium": "", "iv_pct": None, "spot": None}
    iv = vol.realized_vol_pct(symbol, session=session)
    spot = _last_close(symbol, session)
    if iv is not None:
        out["iv_pct"] = round(iv, 1)
    if iv is None or spot is None:
        out["spot"] = spot
        return out
    dte = max(1, (expiry - date.today()).days)
    try:
        q = black_scholes.suggest_strike(
            spot=spot, vol_pct=iv, days_to_expiry=dte,
            target_delta=_SUGGEST_DELTA, kind=kind, rate=settings.risk_free_rate,
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("hedge: strike suggest failed for %s: %s", symbol, exc)
        out["spot"] = spot
        return out
    strike = _round_strike(q.strike)
    out.update(strike=f"{strike:g}", premium=str(round(q.price * 100)), spot=round(spot, 2))
    return out


def _default_symbol(session: Session) -> str:
    """A sensible starting symbol — the first watched name with a price."""
    try:
        items = list_watchlist(session=session)
    except Exception:  # noqa: BLE001
        return ""
    with_price = [it for it in items if it.last_close]
    pick = with_price[0] if with_price else (items[0] if items else None)
    return pick.symbol if pick else ""


def _suggest_defaults(session: Session, *, symbol: str, kind: str, side: str) -> dict[str, object]:
    """Full default/suggestion payload for the form (or the suggest fragment)."""
    expiry = _next_monthly_opex(date.today())
    d: dict[str, object] = {
        "symbol": symbol,
        "option_side": side,
        "option_kind": kind,
        "contracts": 1,
        "expiry": expiry.isoformat(),
        "band_mode": WHALLEY_WILMOTT,
        "risk_aversion": "0.05",
        "fixed_band_shares": "5",
        "pct_move": "0.01",
        "strike": "",
        "premium": "",
        "iv_pct": None,
        "spot": None,
    }
    if symbol:
        d.update(_suggest_trade(symbol, kind, expiry, session))
    return d


# ---------------------------------------------------------------------------
# View helpers
# ---------------------------------------------------------------------------
def _heartbeat_view(hb: dict | None) -> dict:
    if hb is None:
        return {"present": False, "alive": False, "status": "never run", "last": None}
    last = hb.get("last_heartbeat_at")
    alive = last is not None and hb.get("status") == "running" and (
        datetime.now(UTC) - last
    ).total_seconds() < 60
    return {
        "present": True, "alive": alive, "status": hb.get("status"),
        "feed_kind": hb.get("feed_kind"), "active_symbols": hb.get("active_symbols"),
        "pid": hb.get("pid"), "last": last,
    }


def _rows(positions: list[store.HedgePosition]) -> list[dict]:
    return [{"pos": p, "pnl": service.live_pnl(p)} for p in positions]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@router.get("/hedge")
def hedge_index(request: Request, s: Session = Depends(get_session)):
    positions = store.list_hedge_positions(session=s)
    active = [p for p in positions if p.status != "closed"]
    closed = [p for p in positions if p.status == "closed"]
    defaults = _suggest_defaults(s, symbol=_default_symbol(s), kind="call", side="short")
    return render(
        request,
        "hedge/index.html",
        active_rows=_rows(active),
        closed_rows=_rows(closed),
        heartbeat=_heartbeat_view(store.get_heartbeat(session=s)),
        d=defaults,
    )


@router.get("/hedge/suggest")
def hedge_suggest(
    request: Request,
    symbol: str = "",
    option_kind: str = "call",
    option_side: str = "short",
    s: Session = Depends(get_session),
):
    """HTMX: recompute the strike/expiry/premium fields for the typed symbol."""
    kind = option_kind if option_kind in {"call", "put"} else "call"
    side = option_side if option_side in {"short", "long"} else "short"
    sym = (symbol or "").strip().upper()
    if sym and not _SYMBOL_RE.match(sym):
        sym = ""
    return render(request, "hedge/_suggest_fields.html",
                  d=_suggest_defaults(s, symbol=sym, kind=kind, side=side))


@router.post("/hedge")
def hedge_create(
    request: Request,
    symbol: str = Form(...),
    option_kind: str = Form(...),
    option_side: str = Form(...),
    strike: str = Form(...),
    contracts: str = Form(...),
    expiry: str = Form(...),
    premium: str = Form(...),
    band_mode: str = Form(WHALLEY_WILMOTT),
    risk_aversion: str = Form("0.05"),
    fixed_band_shares: str = Form("5"),
    pct_move: str = Form("0.01"),
    s: Session = Depends(get_session),
):
    """Create a fake option position and enable delta hedging on it."""
    try:
        sym = _sanitize_symbol(symbol)
        if option_kind not in {"call", "put"}:
            raise _FormError("Option kind must be call or put.")
        if option_side not in {"short", "long"}:
            raise _FormError("Option side must be short or long.")
        if band_mode not in _VALID_MODES:
            raise _FormError("Unknown band policy.")

        strike_d = _parse_decimal(strike, field="Strike", min_value=0.01, max_value=1_000_000)
        premium_d = _parse_decimal(premium, field="Premium", allow_zero=True, max_value=1e12)
        contracts_i = int(_parse_decimal(contracts, field="Contracts", min_value=1, max_value=_MAX_CONTRACTS))

        try:
            exp_date = datetime.strptime(expiry.strip(), "%Y-%m-%d").date()
        except ValueError:
            raise _FormError("Expiry must be a date (YYYY-MM-DD).") from None
        expiry_dt = datetime.combine(exp_date, time(20, 0), tzinfo=UTC)
        if expiry_dt <= datetime.now(UTC):
            raise _FormError("Expiry must be in the future.")
        if expiry_dt > datetime.now(UTC) + timedelta(days=1000):
            raise _FormError("Expiry is too far out (max ~3 years).")

        policy = HedgePolicy(
            mode=band_mode,
            risk_aversion=_parse_float(risk_aversion, field="Risk aversion", lo=1e-6, hi=1e6),
            fixed_band_shares=_parse_float(fixed_band_shares, field="Fixed band", lo=1, hi=1e9),
            pct_move=_parse_float(pct_move, field="Percent-move", lo=1e-4, hi=1.0),
        )
    except _FormError as exc:
        return flash_redirect("/hedge", "error", str(exc))

    iv_pct = vol.realized_vol_pct(sym, session=s)
    if iv_pct is None:
        return flash_redirect(
            "/hedge", "error",
            f"No price history for {sym} — can't derive volatility. Backfill bars first.",
        )

    hedge_id = store.create_hedge_position(
        symbol=sym, option_kind=option_kind, option_side=option_side,
        strike=strike_d, contracts=contracts_i, expiry=expiry_dt, premium=premium_d,
        iv_pct=round(iv_pct, 4), rate_pct=round(settings.risk_free_rate * 100.0, 4),
        band_policy=policy.to_dict(), session=s,
    )
    return flash_redirect(
        f"/hedge/{hedge_id}", "success",
        f"Hedging {option_side} {contracts_i}x {sym} {option_kind} "
        f"{strike_d:g} (vol ~{iv_pct:.1f}%). Make sure the daemon is running.",
    )


@router.post("/hedge/{hedge_position_id}/toggle")
def hedge_toggle(hedge_position_id: int, request: Request, s: Session = Depends(get_session)):
    pos = store.get_hedge_position(hedge_position_id, session=s)
    if pos is None:
        return flash_redirect("/hedge", "error", "Hedge position not found.")
    if pos.status == "closed":
        return flash_redirect(f"/hedge/{hedge_position_id}", "warn", "Position is closed.")
    new_status = "paused" if pos.status == "active" else "active"
    store.set_status(hedge_position_id, new_status, session=s)
    return flash_redirect(
        f"/hedge/{hedge_position_id}", "success",
        f"Hedging {'paused' if new_status == 'paused' else 'resumed'}.",
    )


@router.post("/hedge/{hedge_position_id}/close")
def hedge_close(
    hedge_position_id: int,
    request: Request,
    close_spot: str = Form(""),
    s: Session = Depends(get_session),
):
    pos = store.get_hedge_position(hedge_position_id, session=s)
    if pos is None:
        return flash_redirect("/hedge", "error", "Hedge position not found.")
    if pos.status == "closed":
        return flash_redirect(f"/hedge/{hedge_position_id}", "warn", "Already closed.")

    spot: float | None = None
    if close_spot.strip():
        try:
            spot = float(_parse_decimal(close_spot, field="Close price", min_value=0.01))
        except _FormError as exc:
            return flash_redirect(f"/hedge/{hedge_position_id}", "error", str(exc))
    elif pos.last_spot is not None:
        spot = float(pos.last_spot)
    if spot is None or spot <= 0:
        return flash_redirect(
            f"/hedge/{hedge_position_id}", "error",
            "No spot available to settle — enter a close price.",
        )

    settlement = service.settle_and_close(pos, spot=spot, reason="manual_close", session=s)
    return flash_redirect(
        f"/hedge/{hedge_position_id}", "success",
        f"Closed at {spot:,.2f} — booked P&L {settlement.total_realized_pnl:+,.2f}.",
    )


# ---------------------------------------------------------------------------
# Playground — run offline hedge experiments and view results
# ---------------------------------------------------------------------------
def _opt_float(raw: str, *, field: str) -> float | None:
    if raw is None or not str(raw).strip():
        return None
    return float(_parse_decimal(raw, field=field, allow_zero=True))


def _spark(values: list[float], *, width: int = 620, height: int = 56) -> dict:
    """SVG polyline geometry for a series, auto-scaled to its own range."""
    if not values:
        return {"points": "", "lo": 0.0, "hi": 0.0, "last": 0.0, "width": width, "height": height, "zero_y": None}
    lo, hi = min(values), max(values)
    rng = (hi - lo) or 1.0
    n = len(values)
    if n == 1:
        pts = f"0,{height / 2:.1f} {width},{height / 2:.1f}"
    else:
        pts = " ".join(f"{i / (n - 1) * width:.1f},{height - (v - lo) / rng * height:.1f}" for i, v in enumerate(values))
    zero_y = height - (0.0 - lo) / rng * height if lo <= 0 <= hi else None
    return {"points": pts, "lo": lo, "hi": hi, "last": values[-1], "width": width, "height": height, "zero_y": zero_y}


def _histogram(samples: list[float], *, bins: int = 24, width: int = 620, height: int = 120) -> dict:
    lo, hi = min(samples), max(samples)
    rng = (hi - lo) or 1.0
    counts = [0] * bins
    for x in samples:
        counts[min(bins - 1, int((x - lo) / rng * bins))] += 1
    mx = max(counts) or 1
    bars = [{"x": i / bins * width, "w": width / bins * 0.88, "h": c / mx * height, "count": c}
            for i, c in enumerate(counts)]
    zero_x = (0.0 - lo) / rng * width if lo <= 0 <= hi else None
    return {"bars": bars, "lo": lo, "hi": hi, "width": width, "height": height, "zero_x": zero_x}


def _playground_form(overrides: dict | None = None) -> dict:
    f = {
        "source": "synthetic", "symbol": "", "from_date": "", "to_date": "",
        "spot": "1300", "vol": "40", "drift": "0", "days": "", "steps_per_day": "39", "seed": "1",
        "option_side": "short", "option_kind": "call", "strike": "", "delta": "0.30",
        "contracts": "1", "dte": "30", "iv": "", "premium": "",
        "band_mode": "whalley_wilmott", "risk_aversion": "0.05", "fixed_band": "5",
        "pct_move": "0.01", "cost_rate": "0.0005",
        "experiment": "single", "n_paths": "500", "sweep_over": "risk_aversion",
        "sweep_values": "0.005,0.02,0.05,0.2,1.0",
    }
    if overrides:
        f.update({k: v for k, v in overrides.items() if v is not None})
    return f


@router.get("/hedge/playground")
def playground_get(request: Request, s: Session = Depends(get_session)):
    return render(request, "hedge/playground.html", f=_playground_form(), result=None, error=None)


@router.post("/hedge/playground")
def playground_run(
    request: Request,
    source: str = Form("synthetic"),
    symbol: str = Form(""),
    from_date: str = Form(""),
    to_date: str = Form(""),
    spot: str = Form("1300"),
    vol: str = Form("40"),
    drift: str = Form("0"),
    days: str = Form(""),
    steps_per_day: str = Form("39"),
    seed: str = Form("1"),
    option_side: str = Form("short"),
    option_kind: str = Form("call"),
    strike: str = Form(""),
    delta: str = Form("0.30"),
    contracts: str = Form("1"),
    dte: str = Form("30"),
    iv: str = Form(""),
    premium: str = Form(""),
    band_mode: str = Form("whalley_wilmott"),
    risk_aversion: str = Form("0.05"),
    fixed_band: str = Form("5"),
    pct_move: str = Form("0.01"),
    cost_rate: str = Form("0.0005"),
    experiment: str = Form("single"),
    n_paths: str = Form("500"),
    sweep_over: str = Form("risk_aversion"),
    sweep_values: str = Form("0.005,0.02,0.05,0.2,1.0"),
    s: Session = Depends(get_session),
):
    """Run a single sim / Monte Carlo / sweep and render the result inline."""
    from stockscan.hedge import simulate as sim
    from stockscan.hedge.policy import HedgePolicy

    raw = {k: v for k, v in locals().items() if isinstance(v, str)}
    f = _playground_form(raw)
    result: dict | None = None
    error: str | None = None
    is_hist = source == "historical"

    try:
        sym = _sanitize_symbol(symbol) if is_hist else None
        spot_v = None if is_hist else _parse_float(spot, field="Spot", lo=0.01, hi=1e7)
        vol_v = _parse_float(vol, field="Vol", lo=0.1, hi=1000)
        drift_v = _parse_signed_float(drift, field="Drift", lo=-1000, hi=1000)
        seed_i = int(_parse_decimal(seed, field="Seed", allow_zero=True, max_value=1e9))
        steps_i = int(_parse_decimal(steps_per_day, field="Steps/day", min_value=1, max_value=390))
        contracts_i = int(_parse_decimal(contracts, field="Contracts", min_value=1, max_value=_MAX_CONTRACTS))
        dte_i = int(_parse_decimal(dte, field="DTE", min_value=1, max_value=1000))
        cost_v = float(_parse_decimal(cost_rate, field="Cost rate", allow_zero=True, max_value=1.0)) if cost_rate.strip() else 0.0005
        policy = HedgePolicy(
            mode=band_mode,
            risk_aversion=_parse_float(risk_aversion, field="Risk aversion", lo=1e-6, hi=1e6),
            fixed_band_shares=_parse_float(fixed_band, field="Fixed band", lo=1, hi=1e9),
            pct_move=_parse_float(pct_move, field="Percent-move", lo=1e-4, hi=1.0),
            cost_rate=cost_v,
        )
        common = dict(
            symbol=sym, from_date=(from_date or None) if is_hist else None,
            to_date=(to_date or None) if is_hist else None, spot=spot_v,
            annual_vol_pct=vol_v, drift_pct=drift_v, days=_opt_float(days, field="Days"),
            steps_per_day=steps_i, seed=seed_i, option_side=option_side, option_kind=option_kind,
            strike=_opt_float(strike, field="Strike"), target_delta=_parse_float(delta, field="Delta", lo=0.01, hi=0.99),
            contracts=contracts_i, dte=dte_i, iv_pct=_opt_float(iv, field="IV"),
            premium=_opt_float(premium, field="Premium"),
        )

        if experiment == "montecarlo":
            n = int(_parse_decimal(n_paths, field="Paths", min_value=1, max_value=20000))
            base_spot = spot_v if spot_v is not None else _last_close(sym, s) if sym else None
            if base_spot is None:
                raise _FormError("Monte Carlo needs a start price (use synthetic, or a symbol with bars).")
            spec = sim.build_option_spec(
                option_kind=option_kind, option_side=option_side, spot=base_spot, dte=dte_i,
                iv_pct=common["iv_pct"] if common["iv_pct"] is not None else vol_v,
                rate_pct=settings.risk_free_rate * 100.0, contracts=contracts_i,
                strike=common["strike"], target_delta=common["target_delta"], premium=common["premium"],
            )
            mc = sim.monte_carlo(spec, policy, n_paths=n, s0=base_spot, annual_vol_pct=vol_v,
                                 drift_pct=drift_v, days=common["days"] or float(dte_i),
                                 steps_per_day=max(10, steps_i // 2), seed=seed_i, cost_rate=cost_v)
            result = {"kind": "montecarlo", "mc": mc, "hist": _histogram(mc["samples"]),
                      "spec": {"option": f"{option_side} {contracts_i}x {option_kind}", "premium": spec.premium}}
        elif experiment == "sweep":
            grid = [float(v) for v in sweep_values.split(",") if v.strip()]
            variations = (sim.fixed_band_grid(grid, cost_rate=cost_v) if sweep_over == "fixed_band"
                          else sim.ww_risk_aversion_grid(grid, cost_rate=cost_v))
            spec, path = sim.resolve_spec_and_path(**common)
            sw = sim.sweep(spec, variations, path=path, cost_rate=cost_v)
            max_abs = max((abs(r["net_pnl"]) for r in sw["rows"]), default=1) or 1
            result = {"kind": "sweep", "sweep": sw, "over": sweep_over, "max_abs": max_abs,
                      "spec": {"option": f"{spec.option_side} {spec.contracts}x {spec.option_kind} {spec.strike:g}"}}
        else:
            spec, path = sim.resolve_spec_and_path(**common)
            res = sim.simulate_hedge(spec, policy, path, cost_rate=cost_v)
            ser = res.sampled_series()
            result = {
                "kind": "single", "summary": res.summary, "trades": res.trades[:100],
                "n_trades": len(res.trades),
                "chart": {
                    "spot": _spark([p["spot"] for p in ser]),
                    "held": _spark([float(p["held"]) for p in ser]),
                    "net": _spark([p["net_pnl"] for p in ser]),
                },
            }
    except _FormError as exc:
        error = str(exc)
    except ValueError as exc:
        error = f"Could not run: {exc}"

    return render(request, "hedge/playground.html", f=f, result=result, error=error)


@router.get("/hedge/{hedge_position_id}")
def hedge_detail(hedge_position_id: int, request: Request, s: Session = Depends(get_session)):
    pos = store.get_hedge_position(hedge_position_id, session=s)
    if pos is None:
        return flash_redirect("/hedge", "error", "Hedge position not found.")
    return render(
        request,
        "hedge/detail.html",
        pos=pos,
        pnl_data=service.live_pnl(pos),
        adjustments=store.list_adjustments(hedge_position_id, session=s),
        heartbeat=_heartbeat_view(store.get_heartbeat(session=s)),
    )
