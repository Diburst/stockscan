"""Options-trading-flavored context: earnings + level proximity + observations.

This module pulls together the stuff an options trader cares about
that ISN'T already in the trend / vol / momentum readings:

  * **Days-to-earnings** from the earnings_calendar table - option
    premium expands ahead of earnings; trades sized through earnings
    behave very differently from no-event holds.
  * **Nearest support / resistance** with distance % - directly drives
    strike-selection ("is the $150 put 2% away or 8% away?").
  * **Curated observations** - short bullets rolling up the trend +
    vol + level state into option-strategy hints.

NOT included (would require option chain data we don't have):
  * Implied volatility (we use realized HV percentile as a proxy).
  * Greeks-friendly framing (delta/theta).
  * Implied move (we use realized-vol-derived expected range).

When option chain integration ships, this module is the natural home
to add those - the dataclass is already shaped for it.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING

from sqlalchemy import text

from stockscan.analysis import black_scholes
from stockscan.analysis.state import (
    Level,
    OptionsContext,
    OptionStrike,
    StrikeSet,
)
from stockscan.config import settings
from stockscan.data.macro_store import latest_macro_value

if TYPE_CHECKING:
    from datetime import date as _date

    from sqlalchemy.orm import Session

    from stockscan.analysis.state import (
        TrendState,
        VolatilityState,
    )

log = logging.getLogger(__name__)

# Strike-suggestion tenors: (days_to_expiry, target_delta). One StrikeSet is
# produced per row, nearest-expiry first. The defaults map to a weekend
# trade-planning workflow: ~6 days = this coming Friday, ~13 days = the next
# Friday, ~30 days = about a month out. Edit this tuple to change the ladder.
_STRIKE_TENORS: tuple[tuple[int, float], ...] = (
    (6, 0.15),
    (13, 0.15),
    (30, 0.20),
)

# A suggested strike is "at" a level/MA when it sits within this multiple of
# ATR(14) of it — a volatility-scaled notion of "close" (per Thomas).
_CONFLUENCE_ATR_MULT = 0.5

# FRED series for the risk-free rate: 1-month constant-maturity Treasury, the
# closest tenor to our ≤30-day options. Read from the cached macro_series
# table (refreshed by `stockscan refresh macro`); falls back to
# settings.risk_free_rate when absent. Stored by FRED in percent (e.g. 5.31).
_RISK_FREE_SERIES = "DGS1MO"


def compute_options_context(
    *,
    symbol: str,
    as_of: _date,
    last_close: float | None,
    levels: list[Level],
    trend: TrendState,
    volatility: VolatilityState,
    session: Session | None = None,
) -> OptionsContext:
    """Build the options-trading framing for one symbol.

    Soft-fails to ``OptionsContext.unavailable()`` only on hard errors;
    otherwise builds whatever fields are available.
    """
    if last_close is None or last_close <= 0:
        return OptionsContext.unavailable()

    # ---- Days to earnings (from earnings_calendar) ----
    days_to_earn: int | None = None
    earn_date: _date | None = None
    if session is not None:
        try:
            row = session.execute(
                text(
                    """
                    SELECT MIN(report_date)::date AS next_earn
                    FROM earnings_calendar
                    WHERE symbol = :s AND report_date >= :d
                    """
                ),
                {"s": symbol, "d": as_of},
            ).first()
            if row is not None and row[0] is not None:
                earn_date = row[0]
                days_to_earn = (earn_date - as_of).days
        except Exception as exc:
            log.debug("options_context: earnings lookup failed for %s: %s", symbol, exc)
    earnings_warning = days_to_earn is not None and days_to_earn <= 7

    # ---- Nearest support / resistance ----
    supports = sorted(
        [lv for lv in levels if lv.kind == "support" and lv.price < last_close],
        key=lambda lv: last_close - lv.price,
    )
    resistances = sorted(
        [lv for lv in levels if lv.kind == "resistance" and lv.price > last_close],
        key=lambda lv: lv.price - last_close,
    )
    nearest_support = supports[0] if supports else None
    nearest_resistance = resistances[0] if resistances else None

    pct_to_support = None
    pct_to_resistance = None
    if nearest_support is not None and last_close > 0:
        pct_to_support = (last_close - nearest_support.price) / last_close * 100
    if nearest_resistance is not None and last_close > 0:
        pct_to_resistance = (nearest_resistance.price - last_close) / last_close * 100

    # ---- Black-Scholes suggested strikes (one StrikeSet per tenor) ----
    # Each tenor's put + call is priced off the EWMA Yang-Zhang forward vol as
    # an IV proxy (no chain), at the tenor-matched FRED risk-free rate, then
    # each strike is checked for confluence with key EMAs and S/R levels.
    # Soft-fails to [] if vol is missing.
    strike_sets = _build_strike_sets(
        symbol=symbol,
        as_of=as_of,
        last_close=last_close,
        levels=levels,
        trend=trend,
        volatility=volatility,
        session=session,
    )

    # ---- Observations ----
    observations = _build_observations(
        last_close=last_close,
        nearest_support=nearest_support,
        nearest_resistance=nearest_resistance,
        pct_to_support=pct_to_support,
        pct_to_resistance=pct_to_resistance,
        trend=trend,
        volatility=volatility,
        days_to_earnings=days_to_earn,
        strike_sets=strike_sets,
    )

    return OptionsContext(
        available=True,
        days_to_earnings=days_to_earn,
        earnings_date=earn_date,
        earnings_warning=earnings_warning,
        nearest_support=nearest_support,
        nearest_resistance=nearest_resistance,
        pct_to_support=round(pct_to_support, 4) if pct_to_support is not None else None,
        pct_to_resistance=round(pct_to_resistance, 4) if pct_to_resistance is not None else None,
        strike_sets=strike_sets,
        observations=observations,
    )


def _build_strike_sets(
    *,
    symbol: str,
    as_of: _date,
    last_close: float,
    levels: list[Level],
    trend: TrendState,
    volatility: VolatilityState,
    session: Session | None = None,
) -> list[StrikeSet]:
    """Solve a put + call for every tenor in ``_STRIKE_TENORS``.

    Each strike is priced off the EWMA Yang-Zhang forward vol (an IV proxy —
    we have no option chain) at the FRED risk-free rate, then annotated with
    any structural confluence (key EMA / S/R level within
    ``_CONFLUENCE_ATR_MULT`` × ATR(14)). Returns an empty list when vol is
    unavailable; individual legs soft-fail to None.
    """
    # Prefer the responsive EWMA-YZ forward vol; fall back to the trailing
    # 21-day HV if it's missing.
    vol_pct = volatility.ewma_vol_pct or volatility.realized_vol_21d_pct
    if not volatility.available or vol_pct is None:
        return []
    rate = _risk_free_rate(as_of, session)
    atr14 = volatility.atr_14  # may be None → confluence check is skipped

    sets: list[StrikeSet] = []
    for days, delta in _STRIKE_TENORS:
        legs: dict[str, OptionStrike | None] = {"call": None, "put": None}
        for kind in ("call", "put"):
            try:
                q = black_scholes.suggest_strike(
                    spot=last_close,
                    vol_pct=vol_pct,
                    days_to_expiry=days,
                    target_delta=delta,
                    kind=kind,
                    rate=rate,
                )
                confluences = _strike_confluences(
                    strike=q.strike, levels=levels, trend=trend, atr14=atr14
                )
                legs[kind] = OptionStrike(
                    kind=q.kind,
                    strike=q.strike,
                    pct_otm=q.pct_otm,
                    target_delta=q.target_delta,
                    delta=q.delta,
                    price=q.price,
                    theta=q.theta,
                    vega=q.vega,
                    gamma=q.gamma,
                    days_to_expiry=q.days_to_expiry,
                    vol_pct=q.vol_pct,
                    rate_pct=q.rate_pct,
                    confluences=confluences,
                )
            except Exception as exc:
                log.debug(
                    "options_context: %s %dd strike solve failed for %s: %s",
                    kind, days, symbol, exc,
                )
        expiry = as_of + timedelta(days=days)
        sets.append(
            StrikeSet(
                days_to_expiry=days,
                target_delta=delta,
                expiry_date=expiry,
                label=f"{days}-day · {expiry:%b %d} ({expiry:%a})",
                call=legs["call"],
                put=legs["put"],
            )
        )
    return sets


def _risk_free_rate(as_of: _date, session: Session | None) -> float:
    """Risk-free rate (decimal) from the cached 1-month Treasury, FRED-sourced.

    Reads the most-recent ``DGS1MO`` print at-or-before ``as_of`` from the
    ``macro_series`` table (no network — refreshed by ``stockscan refresh
    macro``). FRED stores it in percent, so we divide by 100. Falls back to
    ``settings.risk_free_rate`` when there's no session, no print on file, or
    the value looks implausible.
    """
    fallback = settings.risk_free_rate
    if session is None:
        return fallback
    try:
        val = latest_macro_value(_RISK_FREE_SERIES, as_of, session=session)
    except Exception as exc:
        log.debug("options_context: risk-free lookup failed: %s", exc)
        return fallback
    if val is None:
        return fallback
    rate = float(val) / 100.0
    # Sanity band: a 1-month T-bill outside 0–25% is a data error, not a rate.
    return rate if 0.0 <= rate < 0.25 else fallback


def _strike_confluences(
    *,
    strike: float,
    levels: list[Level],
    trend: TrendState,
    atr14: float | None,
) -> tuple[str, ...]:
    """Flag EMAs / S/R levels sitting within 0.5×ATR(14) of ``strike``.

    Returns short prose strings (one per nearby reference), ordered by
    proximity. Empty when ATR is unknown or the strike lands in open space.
    A strike that coincides with structure is a double-edged flag: it's a
    natural magnet/pin (good for a short option to expire near) but also a
    spot where price tends to react, so a sold strike there can get tested.
    """
    if atr14 is None or atr14 <= 0 or strike <= 0:
        return ()
    band = _CONFLUENCE_ATR_MULT * atr14

    hits: list[tuple[float, str]] = []  # (distance, label)

    # Key EMAs.
    if trend.available:
        for period in sorted(trend.emas):
            value = trend.emas.get(period)
            if value is None or value <= 0:
                continue
            dist = abs(strike - value)
            if dist <= band:
                pct = (value - strike) / strike * 100
                hits.append(
                    (dist, f"{period} EMA ${value:.2f} ({abs(pct):.1f}% away)")
                )

    # Support / resistance levels.
    for lv in levels:
        dist = abs(strike - lv.price)
        if dist <= band:
            pct = (lv.price - strike) / strike * 100
            flip = " ex-resistance" if (lv.is_flipped and lv.kind == "support") else (
                " ex-support" if (lv.is_flipped and lv.kind == "resistance") else ""
            )
            hits.append(
                (
                    dist,
                    f"{lv.kind} ${lv.price:.2f}{flip} "
                    f"(str {lv.strength:.2f}, {abs(pct):.1f}% away)",
                )
            )

    hits.sort(key=lambda h: h[0])
    return tuple(label for _, label in hits)


def _build_observations(
    *,
    last_close: float,
    nearest_support: Level | None,
    nearest_resistance: Level | None,
    pct_to_support: float | None,
    pct_to_resistance: float | None,
    trend: TrendState,
    volatility: VolatilityState,
    days_to_earnings: int | None,
    strike_sets: list[StrikeSet] | None = None,
) -> list[str]:
    """Compose the bullet observations shown in the dashboard card.

    Each observation is a short prose statement combining multiple
    state inputs into a single readable sentence. Keep them factual
    and actionable - these get read at a glance every morning.
    """
    obs: list[str] = []

    # Earnings warning is the highest-priority observation when present.
    if days_to_earnings is not None:
        if days_to_earnings <= 0:
            obs.append("Earnings reported on or before today - IV is likely already crushed.")
        elif days_to_earnings <= 5:
            obs.append(
                f"Earnings in {days_to_earnings} day{'s' if days_to_earnings != 1 else ''} - "
                f"premium is elevated; size positions through earnings carefully."
            )
        elif days_to_earnings <= 30:
            obs.append(
                f"Earnings in {days_to_earnings} days - within typical short-term option window. "
                f"Realized vol may understate the true expected move."
            )

    # Trend + level proximity for strike selection.
    if trend.available and trend.bucket in ("strong_up", "up"):
        if pct_to_support is not None and pct_to_support < 3:
            obs.append(
                f"Bullish trend AND price near support (~{pct_to_support:.1f}% above "
                f"${nearest_support.price:.2f}). Cash-secured short puts at or below "
                f"that level have a favorable risk profile."
            )
        elif pct_to_resistance is not None and pct_to_resistance < 3:
            obs.append(
                f"Bullish trend but price right at resistance (~{pct_to_resistance:.1f}% "
                f"below ${nearest_resistance.price:.2f}). Wait for a clean break or "
                f"sell call spreads above that level."
            )
        elif pct_to_support is not None and pct_to_resistance is not None:
            obs.append(
                f"Bullish trend, price between support ${nearest_support.price:.2f} "
                f"({pct_to_support:.1f}% below) and resistance "
                f"${nearest_resistance.price:.2f} ({pct_to_resistance:.1f}% above). "
                f"Trade with the trend; tighten exits near resistance."
            )
    elif trend.available and trend.bucket in ("strong_down", "down"):
        if pct_to_resistance is not None and pct_to_resistance < 3:
            obs.append(
                f"Bearish trend AND price near resistance (~{pct_to_resistance:.1f}% "
                f"below ${nearest_resistance.price:.2f}). Bear call spreads above that "
                f"level offer favorable risk:reward."
            )
        elif pct_to_support is not None and pct_to_support < 3:
            obs.append(
                f"Bearish trend but price right at support (~{pct_to_support:.1f}% "
                f"above ${nearest_support.price:.2f}). Could see a bear bounce; wait "
                f"for confirmation before adding short delta."
            )
        elif pct_to_support is not None and pct_to_resistance is not None:
            obs.append(
                f"Bearish trend, price between resistance ${nearest_resistance.price:.2f} "
                f"({pct_to_resistance:.1f}% above) and support "
                f"${nearest_support.price:.2f} ({pct_to_support:.1f}% below). "
                f"Short delta favored; book gains near support."
            )
    elif (
        trend.available
        and trend.bucket == "neutral"
        and pct_to_support is not None
        and pct_to_resistance is not None
    ):
        obs.append(
            f"No clear trend; price ranging between ${nearest_support.price:.2f} "
            f"and ${nearest_resistance.price:.2f}. Iron condors with wings outside "
            f"those levels capture premium with the range."
        )

    # Polarity / role-reversal callouts. A broken-resistance-now-support
    # is one of the strongest bullish setups in classical TA (and vice
    # versa for a broken-support-now-resistance) - worth surfacing as
    # its own observation when the flipped level is also nearby.
    if (
        nearest_support is not None
        and nearest_support.is_flipped
        and pct_to_support is not None
        and pct_to_support < 5
    ):
        obs.append(
            f"Nearest support at ${nearest_support.price:.2f} is a former "
            f"resistance that price has broken through (polarity flip). "
            f"Bulls typically defend this kind of level aggressively; "
            f"cash-secured short puts at or below it have a favorable "
            f"risk profile while the level holds."
        )
    if (
        nearest_resistance is not None
        and nearest_resistance.is_flipped
        and pct_to_resistance is not None
        and pct_to_resistance < 5
    ):
        obs.append(
            f"Nearest resistance at ${nearest_resistance.price:.2f} is a "
            f"former support that price has broken below (polarity flip). "
            f"Bears typically defend this kind of level aggressively; "
            f"bear call spreads above it have a favorable risk profile "
            f"while the level holds."
        )

    # Vol regime hint for option strategy selection.
    if volatility.available:
        if volatility.bucket == "low":
            obs.append(
                "HV is at 1-year lows - premium is cheap. Long-vol strategies "
                "(long straddles / strangles ahead of catalysts) get more "
                "attractive in this regime."
            )
        elif volatility.bucket == "high":
            obs.append(
                "HV near 1-year highs - premium is rich and historically "
                "mean-reverts. Selling premium (credit spreads, iron condors) "
                "tends to outperform in this regime, but check for known "
                "catalysts justifying the elevated vol."
            )

    # Expected-range numbers for quick strike-selection reference.
    if volatility.available and volatility.expected_30d is not None:
        er = volatility.expected_30d
        obs.append(
            f"30-day ±1sigma expected range: ${er.low:.2f} - ${er.high:.2f} "
            f"(±{er.sigma_pct:.1f}% / ±${er.sigma_dollars:.2f} from current). "
            f"~68% of historical 30-day moves stay inside this band."
        )

    # Black-Scholes suggested strikes — one strangle per tenor, priced off
    # 21-day realized HV as an IV proxy.
    for ss in strike_sets or []:
        if ss.put is None or ss.call is None:
            continue
        obs.append(
            f"{ss.label}: Δ{ss.target_delta:.2f} short put ${ss.put.strike:.2f} "
            f"({ss.put.pct_otm:.1f}% OTM) / short call ${ss.call.strike:.2f} "
            f"(+{ss.call.pct_otm:.1f}% OTM), off EWMA Yang-Zhang HV."
        )

    # Confluence callout — strikes landing on an EMA or S/R level are
    # natural pin/magnet spots but also where price reacts; surface them.
    confluence_lines: list[str] = []
    for ss in strike_sets or []:
        for leg in (ss.put, ss.call):
            if leg is not None and leg.confluences:
                confluence_lines.append(
                    f"{ss.days_to_expiry}-day {leg.kind} ${leg.strike:.2f} sits near "
                    f"{'; '.join(leg.confluences)}."
                )
    if confluence_lines:
        obs.append(
            "Strike confluence (within 0.5×ATR of structure — a magnet for "
            "expiry but also a level price tends to react at): "
            + " ".join(confluence_lines)
        )

    return obs
