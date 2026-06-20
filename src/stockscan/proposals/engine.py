"""Candidate generation, hard filters, side selection, and scoring.

This is the heart of the proposal engine, and it's meant to read top-to-bottom
like a trader's checklist:

  1. For each watched name, take the nearest-expiry 15Δ strikes from its
     options_context (computed upstream by the analysis engine).
  2. The DAY-COLOR trigger picks the candidate side: a green day suggests
     selling a call, a red day suggests selling a put. No trigger -> no trade.
  3. TREND/LEVEL gating qualifies the side — with-trend put-sales on dips are
     preferred; call-sales are only taken into resistance, and down-weighted
     hard on momentum leaders (don't sell calls into a breakout).
  4. HARD FILTERS drop anything with earnings inside the expiry, thin liquidity,
     a missing IV, or stale/insufficient bars.
  5. A 0–1 SCORE blends premium richness, room to the threatened level, strike
     confluence, trend alignment, and how stretched today's move is.

Knobs are module constants — edit and the next run picks them up.
"""

from __future__ import annotations

from typing import Any

from stockscan.proposals._models import SELL_CALL, SELL_PUT, OptionProposal

# ---- knobs ----------------------------------------------------------------
DAY_TRIGGER_PCT = 1.5      # min |1-day move| to fire the day-color trigger
NEAR_LEVEL_PCT = 4.0       # a strike is "at" a level within this % of spot
PRICE_AT_LEVEL_PCT = 2.5   # current PRICE is "at" the level within this % (context flag)
MIN_DOLLAR_VOLUME = 5_000_000.0   # liquidity floor (last bar $ volume)
MIN_IV_PCT = 20.0          # below this, not worth selling
EARNINGS_BUFFER_DAYS = 2   # drop if earnings land within dte + buffer

# Score weights (sum to 1.0).
W_PREMIUM = 0.30
W_ROOM = 0.20
W_CONFLUENCE = 0.15
W_TREND = 0.25
W_DAYCOLOR = 0.10

# (breakdown key, human label, weight) — drives the UI score-derivation card.
SCORE_INPUTS: tuple[tuple[str, str, float], ...] = (
    ("premium", "Premium — IV richness", W_PREMIUM),
    ("room", "Room to threatened level", W_ROOM),
    ("confluence", "Strike sits on levels", W_CONFLUENCE),
    ("trend_align", "Trend alignment", W_TREND),
    ("daycolor", "Day-color stretch", W_DAYCOLOR),
)

_UPTREND = {"strong_up", "up"}
_DOWNTREND = {"strong_down", "down"}


def _clamp01(x: float) -> float:
    return 0.0 if x < 0 else 1.0 if x > 1 else x


def _day_move_pct(analysis: Any) -> float | None:
    """1-day % change from the analysis' close history (server-side only)."""
    hist = getattr(analysis, "closes_history", None) or []
    if len(hist) < 2:
        return None
    prev, last = hist[-2][1], hist[-1][1]
    if not prev:
        return None
    return (last - prev) / prev * 100.0


def _select_side(
    day_move: float, trend_bucket: str, oc: Any
) -> tuple[str, float, float | None] | None:
    """Pick the side from day-color, then qualify it with trend + levels.

    Returns (side, trend_alignment_0to1, pct_to_threatened_level) or None when
    there's no qualifying trigger. ``trend_alignment`` rewards with-trend
    put-sales and penalizes counter-trend call-sales.
    """
    up = trend_bucket in _UPTREND
    down = trend_bucket in _DOWNTREND

    if day_move <= -DAY_TRIGGER_PCT:
        # Red day -> sell a put. Best with-trend (dip in an uptrend).
        align = 1.0 if up else (0.55 if not down else 0.35)
        return SELL_PUT, align, getattr(oc, "pct_to_support", None)

    if day_move >= DAY_TRIGGER_PCT:
        # Green day -> sell a call, but only into resistance, and never into a
        # breakout. Counter-trend (selling calls in an uptrend) is penalized.
        ptr = getattr(oc, "pct_to_resistance", None)
        at_resistance = ptr is not None and ptr <= NEAR_LEVEL_PCT
        if not at_resistance or trend_bucket == "strong_up":
            return None  # green but breaking out / open space -> skip the call sale
        align = 0.45 if up else (1.0 if down else 0.7)
        return SELL_CALL, align, ptr

    return None  # move too small to trigger


def _passes_hard_filters(analysis: Any, dte: int, iv_pct: float | None) -> str | None:
    """Return a rejection reason, or None if the candidate clears every filter."""
    oc = analysis.options_context
    dte_arn = getattr(oc, "days_to_earnings", None)
    if dte_arn is not None and dte_arn <= dte + EARNINGS_BUFFER_DAYS:
        return "earnings_in_expiry"
    dollar_vol = getattr(analysis, "last_volume", None)
    if dollar_vol is not None and dollar_vol < MIN_DOLLAR_VOLUME:
        return "illiquid"
    if iv_pct is None or iv_pct < MIN_IV_PCT:
        return "iv_too_low"
    return None


def _score(
    *, iv_pct: float, pct_to_threat: float | None, confluence_count: int,
    trend_align: float, day_move: float,
) -> tuple[float, dict[str, Any]]:
    """Blend the inputs into a 0–1 attractiveness score (+ breakdown)."""
    premium = _clamp01(iv_pct / 150.0)                      # IV richness
    room = _clamp01((pct_to_threat or 0.0) / 12.0)          # cushion to the level
    confluence = _clamp01(confluence_count / 3.0)           # strike sits on levels
    daycolor = _clamp01(abs(day_move) / 5.0)                # how stretched the move
    score = (
        W_PREMIUM * premium
        + W_ROOM * room
        + W_CONFLUENCE * confluence
        + W_TREND * trend_align
        + W_DAYCOLOR * daycolor
    )
    breakdown = {
        "premium": round(premium, 3),
        "room": round(room, 3),
        "confluence": round(confluence, 3),
        "trend_align": round(trend_align, 3),
        "daycolor": round(daycolor, 3),
        "score": round(score, 4),
    }
    return score, breakdown


def propose_candidates(analyses: list[Any]) -> list[OptionProposal]:
    """Generate scored short-premium candidates from per-symbol analyses.

    One candidate (at most) per symbol — the side the day-color trigger selects.
    Returns them sorted by score descending. Sizing/diversification happens in
    ``portfolio.build_book``.
    """
    out: list[OptionProposal] = []
    for a in analyses:
        if not getattr(a, "available", False):
            continue
        oc = getattr(a, "options_context", None)
        if oc is None or not getattr(oc, "available", False):
            continue
        sets = list(getattr(oc, "strike_sets", None) or [])
        if not sets:
            continue
        nearest = sets[0]

        day_move = _day_move_pct(a)
        if day_move is None:
            continue  # insufficient/stale history -> can't trigger

        trend_bucket = getattr(getattr(a, "trend", None), "bucket", "?")
        sel = _select_side(day_move, trend_bucket, oc)
        if sel is None:
            continue
        side, trend_align, pct_to_threat = sel

        leg = nearest.call if side == SELL_CALL else nearest.put
        if leg is None:
            continue
        iv_pct = getattr(leg, "vol_pct", None)

        reason = _passes_hard_filters(a, nearest.days_to_expiry, iv_pct)
        if reason is not None:
            continue

        confluence_count = len(getattr(leg, "confluences", ()) or ())
        # Context flag (not scored): is PRICE itself at the threatened level now?
        price_at_level = pct_to_threat is not None and pct_to_threat <= PRICE_AT_LEVEL_PCT
        score, breakdown = _score(
            iv_pct=iv_pct, pct_to_threat=pct_to_threat,
            confluence_count=confluence_count, trend_align=trend_align,
            day_move=day_move,
        )

        threat = "resistance" if side == SELL_CALL else "support"
        rationale = (
            f"{'Green' if side == SELL_CALL else 'Red'} day ({day_move:+.1f}%); "
            f"sell {side.split('_')[1]} {leg.strike:g} ({leg.pct_otm:+.0f}% OTM, "
            f"{nearest.days_to_expiry}d), IV~{iv_pct:.0f}%, "
            f"{confluence_count} level confluence(s), "
            f"{(pct_to_threat if pct_to_threat is not None else float('nan')):.0f}% to {threat}."
        )

        out.append(
            OptionProposal(
                symbol=a.symbol,
                side=side,
                expiry_date=getattr(nearest, "expiry_date", None),
                days_to_expiry=nearest.days_to_expiry,
                strike=leg.strike,
                delta=getattr(leg, "delta", 0.0),
                est_credit=getattr(leg, "price", 0.0),
                pct_otm=leg.pct_otm,
                iv_pct=iv_pct,
                score=score,
                size_weight=0.0,  # filled by portfolio.build_book
                day_move_pct=round(day_move, 2),
                days_to_earnings=getattr(oc, "days_to_earnings", None),
                confluence_count=confluence_count,
                pct_to_threat=pct_to_threat,
                trend_bucket=trend_bucket,
                rationale=rationale,
                price_at_level=price_at_level,
                score_breakdown=breakdown,
            )
        )

    out.sort(key=lambda p: p.score, reverse=True)
    return out
