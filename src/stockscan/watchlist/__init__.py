"""User watchlist — manually-tracked symbols with optional price-target alerts."""

from stockscan.watchlist.alerts import check_and_fire_alerts
from stockscan.watchlist.store import (
    DEFAULT_LIST_NAME,
    AddSymbolsResult,
    Watchlist,
    WatchlistItem,
    add_symbols,
    add_to_watchlist,
    create_watchlist,
    delete_watchlist,
    list_watchlist,
    list_watchlists,
    remove_from_list,
    remove_from_watchlist,
    rename_watchlist,
    resolve_selection,
    set_target,
    toggle_alert,
    watchlist_symbols,
)

__all__ = [
    "DEFAULT_LIST_NAME",
    "AddSymbolsResult",
    "Watchlist",
    "WatchlistItem",
    "add_symbols",
    "add_to_watchlist",
    "create_watchlist",
    "delete_watchlist",
    "remove_from_watchlist",
    "remove_from_list",
    "rename_watchlist",
    "resolve_selection",
    "list_watchlist",
    "list_watchlists",
    "set_target",
    "toggle_alert",
    "watchlist_symbols",
    "check_and_fire_alerts",
]
