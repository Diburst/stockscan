"""Candidate generation: trigger, side, hard filters, one rank key.

Read top-to-bottom like a trader's checklist. For each watched name:

  1. STRIKES — take the nearest-expiry 15Δ put and call from its options
     context (computed upstream by the analysis engine; only ``sets[0]``
     reaches this module).
  2. TRIGGER — today's move in units of the name's own daily vol, so a
     1σ day means the same thing for a 20%-vol and an 80%-vol name.
       * Put-sale trigger: the SECTOR-RESIDUAL move ≤ −DAY_TRIGGER_SIGMA.
         Only residual reversal survives in large caps (Da–Liu–Schaumburg;
         Blitz et al.), the same canon rsi2_meanrev rests on. A name down 2%
         on a day its sector is down 2.5% is beta, not a dip. Names without a
         sector composite (no residual) never trigger a put-sale.
       * Call-sale trigger: the RAW move ≥ +DAY_TRIGGER_SIGMA. A green day into
         the strike is a level bet, not a reversal bet — the question is
         whether the name itself has moved the strike far enough away, and
         the sector does not answer that.
       The put check runs first; a name cannot carry both sides.
  3. SIDE × TREND × REGIME — the trend bucket and the regime layer qualify
     the side. Red day in ``strong_down`` → skipped (falling knife). Green
     day in ``strong_up`` → skipped (don't sell calls into a breakout). Gate
     closed → a put-sale is a synthetic long against the tape, alignment
     forced to counter-trend (0.35); call-sales unchanged. Credit stress →
     put-sales skipped outright (short crash insurance while insurance is
     repricing); call-sales pass here and are halved in ``portfolio``.
  4. HARD FILTERS, in order — earnings inside the expiry (only when the
     calendar knows the date; unknown is carried as a flag, not a pass),
     HV percentile below the floor (below it the analysis page says "go long
     vol"), 20-day ADV below the floor (the only options-liquidity proxy we
     have), price below the floor (the 15Δ strike rounds badly and the
     credit is pennies).
  5. RANK — ``rank_key = |move_sigma| × trend_align``, ties broken by HV
     percentile. No weighted blend: the alignment values act as a with-trend
     multiplier on the reversal magnitude.

Two numbers a seller reads ride along on every row: σ-distance of the strike
over the tenor and the annualised credit yield. Knobs are module constants —
edit and the next run picks them up. Rejections are logged at debug.
"""

from __future__ import annotations

import logging
from math import sqrt
from typing import Any

from stockscan.proposals._models import SELL_CALL, SELL_PUT, OptionProposal
from stockscan.regime import MarketRegime

log = logging.getLogger(__name__)

# ---- knobs ----------------------------------------------------------------
DAY_TRIGGER_SIGMA = 1.0        # |1-day move| in units of the name's daily vol
MIN_HV_PERCENTILE = 25         # below this the analysis page says "go long vol"
MIN_ADV_20D = 25_000_000.0     # underlying 20-day ADV as the options-liquidity proxy
MIN_PRICE = 10.0               # below this the 15Δ strike rounds badly, credit is pennies
EARNINGS_BUFFER_DAYS = 2       # drop if earnings land within dte + buffer

COUNTER_TREND_ALIGN = 0.35     # also the forced put alignment while the gate is closed

_UPTREND = {"strong_up", "up"}
_DOWNTREND = {"strong_down", "down"}


def _select_side(
    raw_sigma: float,
    residual_sigma: float | None,
    trend_bucket: str,
    regime: MarketRegime | None,
) -> tuple[str, float, float] | str:
    """Pick the side from the day move, then qualify it with trend and regime.

    Returns ``(side, trend_align, move_sigma)`` or a rejection reason.
    """
    up = trend_bucket in _UPTREND
    down = trend_bucket in _DOWNTREND
    gate_open = regime is None or regime.trend_gate_open
    credit_stress = regime is not None and regime.credit_stress_flag

    if residual_sigma is not None and residual_sigma <= -DAY_TRIGGER_SIGMA:
        # Red day, net of the sector -> sell a put. Best with-trend (dip in an uptrend).
        if trend_bucket == "strong_down":
            return "falling_knife"
        if credit_stress:
            return "credit_stress"
        align = 1.0 if up else (0.55 if not down else COUNTER_TREND_ALIGN)
        if not gate_open:
            align = COUNTER_TREND_ALIGN
        return SELL_PUT, align, residual_sigma

    if raw_sigma >= DAY_TRIGGER_SIGMA:
        # Green day -> sell a call, but never into a breakout. Counter-trend
        # (selling calls in an uptrend) is penalized.
        if trend_bucket == "strong_up":
            return "breakout"
        align = 0.45 if up else (1.0 if down else 0.7)
        return SELL_CALL, align, raw_sigma

    return "no_trigger"


def _passes_hard_filters(analysis: Any, dte: int) -> str | None:
    """Return a rejection reason, or None if the candidate clears every filter."""
    oc = analysis.options_context
    if oc.earnings_known and oc.days_to_earnings is not None:
        if oc.days_to_earnings <= dte + EARNINGS_BUFFER_DAYS:
            return "earnings_in_expiry"
    hv_pct_rank = analysis.volatility.hv_percentile
    if hv_pct_rank is not None and hv_pct_rank < MIN_HV_PERCENTILE:
        return "hv_too_low"
    if analysis.adv_20d is not None and analysis.adv_20d < MIN_ADV_20D:
        return "illiquid"
    if analysis.last_close is not None and analysis.last_close < MIN_PRICE:
        return "price_too_low"
    return None


def sigma_distance(pct_otm: float, hv_pct: float, dte: int) -> float:
    """Strike distance from spot in σ of the tenor's expected move."""
    return abs(pct_otm) / (hv_pct * sqrt(dte / 252.0))


def credit_yield_ann(est_credit: float, strike: float, dte: int) -> float:
    """Estimated credit as an annualised percent of the strike."""
    return est_credit / strike * 365.0 / dte * 100.0


def propose_candidates(
    analyses: list[Any], regime: MarketRegime | None = None
) -> list[OptionProposal]:
    """Generate ranked short-premium candidates from per-symbol analyses.

    One candidate (at most) per symbol — the side the trigger selects. Returns
    them sorted by ``rank_key`` descending. Sizing and diversification happen in
    ``portfolio.build_book``.
    """
    out: list[OptionProposal] = []
    for a in analyses:
        if not a.available or not a.options_context.available:
            continue
        sets = a.options_context.strike_sets
        if not sets or sets[0].days_to_expiry <= 0:
            continue
        nearest = sets[0]

        if a.day_move_pct is None or not a.daily_sigma_pct:
            _reject(a.symbol, "no_move_or_sigma")
            continue
        raw_sigma = a.day_move_pct / a.daily_sigma_pct
        residual_sigma = (
            a.day_move_residual_pct / a.daily_sigma_pct
            if a.day_move_residual_pct is not None else None
        )

        trend_bucket = a.trend.bucket
        sel = _select_side(raw_sigma, residual_sigma, trend_bucket, regime)
        if isinstance(sel, str):
            _reject(a.symbol, sel)
            continue
        side, trend_align, move_sigma = sel

        leg = nearest.call if side == SELL_CALL else nearest.put
        if leg is None:
            _reject(a.symbol, "no_leg")
            continue

        reason = _passes_hard_filters(a, nearest.days_to_expiry)
        if reason is not None:
            _reject(a.symbol, reason)
            continue

        dte = nearest.days_to_expiry
        hv_pct = leg.vol_pct
        hv_rank = a.volatility.hv_percentile
        rank_key = abs(move_sigma) * trend_align
        sig_dist = sigma_distance(leg.pct_otm, hv_pct, dte)
        yield_ann = credit_yield_ann(leg.price, leg.strike, dte)
        earnings_known = a.options_context.earnings_known

        move_desc = (
            f"{move_sigma:+.1f}σ residual, {a.day_move_pct:+.1f}% raw"
            if side == SELL_PUT else f"{move_sigma:+.1f}σ, {a.day_move_pct:+.1f}%"
        )
        rationale = (
            f"{'Green' if side == SELL_CALL else 'Red'} day ({move_desc}); "
            f"sell {side.split('_')[1]} {leg.strike:g} ({leg.pct_otm:+.0f}% OTM, "
            f"{sig_dist:.1f}σ, {dte}d), HV~{hv_pct:.0f}%"
            f"{f' (pct-rank {hv_rank:.0f})' if hv_rank is not None else ''}, "
            f"yield {yield_ann:.0f}%/yr, trend {trend_bucket}"
            f"{'' if earnings_known else '; earnings: unknown'}."
        )

        out.append(
            OptionProposal(
                symbol=a.symbol,
                side=side,
                expiry_date=nearest.expiry_date,
                days_to_expiry=dte,
                strike=leg.strike,
                delta=leg.delta,
                est_credit=leg.price,
                pct_otm=leg.pct_otm,
                hv_pct=hv_pct,
                hv_percentile=hv_rank,
                move_sigma=round(move_sigma, 3),
                trend_align=trend_align,
                rank_key=round(rank_key, 4),
                size_weight=0.0,  # filled by portfolio.build_book
                contracts=None,   # filled by portfolio.build_book
                sigma_distance=round(sig_dist, 2),
                credit_yield_ann=round(yield_ann, 2),
                day_move_pct=round(a.day_move_pct, 2),
                day_move_residual_pct=(
                    round(a.day_move_residual_pct, 2)
                    if a.day_move_residual_pct is not None else None
                ),
                days_to_earnings=a.options_context.days_to_earnings,
                earnings_known=earnings_known,
                trend_bucket=trend_bucket,
                confluences=tuple(leg.confluences),
                rationale=rationale,
                score_breakdown={
                    "move_sigma": round(move_sigma, 3),
                    "trend_align": trend_align,
                    "hv_percentile": hv_rank,
                    "rank_key": round(rank_key, 4),
                },
            )
        )

    out.sort(key=lambda p: (p.rank_key, p.hv_percentile if p.hv_percentile is not None else -1.0), reverse=True)
    return out


def _reject(symbol: str, reason: str) -> None:
    log.debug("options %s: rejected — %s", symbol, reason)
