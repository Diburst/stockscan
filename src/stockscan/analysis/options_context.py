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
from typing import TYPE_CHECKING

from sqlalchemy import text

from stockscan.analysis.state import (
    Level,
    OptionsContext,
)

if TYPE_CHECKING:
    from datetime import date as _date

    from sqlalchemy.orm import Session

    from stockscan.analysis.state import (
        TrendState,
        VolatilityState,
    )

log = logging.getLogger(__name__)


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
        observations=observations,
    )


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

    return obs
