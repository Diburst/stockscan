"""User watchlist — manually-tracked symbols with optional price-target alerts."""

from stockscan.watchlist.alerts import check_and_fire_alerts
from stockscan.watchlist.store import (
    WatchlistItem,
    add_to_watchlist,
    list_watchlist,
    remove_from_watchlist,
    set_target,
    toggle_alert,
    watchlist_symbols,
)

__all__ = [
    "WatchlistItem",
    "add_to_watchlist",
    "remove_from_watchlist",
    "list_watchlist",
    "set_target",
    "toggle_alert",
    "watchlist_symbols",
    "check_and_fire_alerts",
]
