"""Insider transactions (SEC Form 4) — EODHD ``/api/insider-transactions``.

Each upstream call costs 10 API credits, so the package is structured
around a strict cooldown gate:

  * ``refresh_insider_for_watchlist`` — single watchlist-wide refresh,
    runs at most once per 23 hours (tracked in ``insider_refresh_log``).
    Wired into the watchlist's "Refresh bars" button so cumulative call
    cost is bounded regardless of how often the user clicks refresh.

  * ``refresh_insider_for_symbol`` — on-demand per-symbol from the
    analysis page, with its own per-scope 23h cooldown.

  * ``net_buys_90d(symbol)`` — UI aggregation surfaced as a small pill on
    the watchlist and a card on analysis detail.

The signal-value asymmetry (buys = high signal, sells = noisy) is
classical; we store both unfiltered and compute net = P − S in the UI
helpers.
"""

from __future__ import annotations

from stockscan.insider.cooldown import (
    REFRESH_COOLDOWN_HOURS,
    can_refresh,
    finish_refresh,
    last_successful_refresh,
    start_refresh,
)
from stockscan.insider.refresh import (
    InsiderRefreshResult,
    refresh_insider_for_symbol,
    refresh_insider_for_watchlist,
)
from stockscan.insider.store import (
    InsiderTransaction,
    net_buys_90d,
    recent_transactions,
    upsert_transactions,
)

__all__ = [
    "InsiderRefreshResult",
    "InsiderTransaction",
    "REFRESH_COOLDOWN_HOURS",
    "can_refresh",
    "finish_refresh",
    "last_successful_refresh",
    "net_buys_90d",
    "recent_transactions",
    "refresh_insider_for_symbol",
    "refresh_insider_for_watchlist",
    "start_refresh",
    "upsert_transactions",
]
