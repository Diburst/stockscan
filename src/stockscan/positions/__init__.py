"""Position / trade lifecycle helpers (DESIGN §4.5)."""

from stockscan.positions.paper_store import (
    PaperTrade,
    check_auto_close,
    close_paper_trade,
    get_paper_trade,
    list_closed_paper_trades,
    list_open_paper_trades,
    mark_to_market,
    open_paper_trade,
)
from stockscan.positions.store import (
    Trade,
    get_trade,
    list_closed_trades,
    list_open_trades,
)

__all__ = [
    "Trade",
    "get_trade",
    "list_closed_trades",
    "list_open_trades",
    "PaperTrade",
    "check_auto_close",
    "close_paper_trade",
    "get_paper_trade",
    "list_closed_paper_trades",
    "list_open_paper_trades",
    "mark_to_market",
    "open_paper_trade",
]
