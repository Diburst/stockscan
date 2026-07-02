"""Realized-volatility σ for the hedge's delta calc.

Per the design decision, the hedge derives its Black-Scholes σ automatically
from the symbol's realized volatility — no user-entered IV. This mirrors what
the analysis / options-context layer already does: prefer the responsive EWMA
Yang-Zhang forward vol, fall back to trailing 21-day HV. Live *spot* drives the
delta tick-by-tick; σ is a slower input, refreshed once per day by the daemon.

Returns annualised vol in **percent** (e.g. 28.5), matching
``black_scholes.suggest_strike``'s ``vol_pct`` convention.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from stockscan.analysis.volatility import compute_volatility
from stockscan.data.store import get_bars

log = logging.getLogger(__name__)

# ~15 months of daily bars is comfortably enough for the 63-day YZ / EWMA window.
_LOOKBACK_DAYS = 460


def realized_vol_pct(symbol: str, *, session: Session | None = None) -> float | None:
    """Annualised realized vol (%) for ``symbol``, or ``None`` if unavailable.

    Prefers EWMA Yang-Zhang forward vol, falls back to 21-day HV.
    """
    end = datetime.now(UTC)
    start = end - timedelta(days=_LOOKBACK_DAYS)
    try:
        bars = get_bars(symbol, start, end, session=session)
    except Exception as exc:  # noqa: BLE001 - best-effort; caller handles None
        log.warning("hedge.vol: bar load failed for %s: %s", symbol, exc)
        return None
    vol = compute_volatility(bars)
    if not vol.available:
        return None
    return vol.ewma_vol_pct or vol.realized_vol_21d_pct
