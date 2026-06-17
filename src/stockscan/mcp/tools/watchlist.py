"""Watchlist tools — reads plus the full management surface (writes)."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from stockscan.mcp.serialize import jsonable
from stockscan.watchlist import add_symbols as _add_symbols
from stockscan.watchlist import add_to_watchlist as _add
from stockscan.watchlist import create_watchlist as _create_list
from stockscan.watchlist import delete_watchlist as _delete_list
from stockscan.watchlist import list_watchlist as _list_items
from stockscan.watchlist import list_watchlists as _list_lists
from stockscan.watchlist import remove_from_watchlist as _remove
from stockscan.watchlist import rename_watchlist as _rename_list
from stockscan.watchlist import set_target as _set_target
from stockscan.watchlist import toggle_alert as _toggle_alert


# ---------------------------------------------------------------- reads
def list_watchlists() -> dict[str, Any]:
    """List the named watchlist *lists* with their symbol counts.

    Use this to discover list ids before targeting one with list_watchlist,
    analyze_watchlist, or add_symbols.

    Returns:
        {"lists": [{list_id, name, count}, ...]}.
    """
    return {"lists": [jsonable(w) for w in _list_lists()]}


def list_watchlist(list_id: int | None = None) -> dict[str, Any]:
    """List watchlist items, enriched with latest close and % change.

    Args:
        list_id: Restrict to one named list's members; None = every symbol.

    Returns:
        {"count", "items": [{watchlist_id, symbol, target_price,
        target_direction, alert_enabled, note, last_close, ...}, ...]}.
    """
    items = _list_items(list_id=list_id)
    return {"count": len(items), "items": [jsonable(i) for i in items]}


# --------------------------------------------------------------- writes
def add_to_watchlist(
    symbol: str,
    target_price: float | None = None,
    target_direction: str | None = None,
    note: str | None = None,
    list_id: int | None = None,
) -> dict[str, Any]:
    """Add a single symbol to the watchlist (idempotent on symbol). WRITE.

    Args:
        symbol: Ticker to add (e.g. "NVDA").
        target_price: Optional price-alert target; set together with direction.
        target_direction: "above" or "below".
        note: Optional free-text note.
        list_id: Optional existing list id; defaults to the primary watchlist.

    Returns:
        {"ok": True, "watchlist_id", "symbol"} or {"error": ...}.
    """
    tp: Decimal | None = None
    if target_price is not None:
        try:
            tp = Decimal(str(target_price))
        except (InvalidOperation, ValueError):
            return {"error": "invalid_target_price", "target_price": target_price}
    try:
        wid = _add(
            symbol,
            target_price=tp,
            target_direction=target_direction,  # type: ignore[arg-type]
            note=note,
            list_id=list_id,
        )
    except ValueError as exc:
        return {"error": "invalid_request", "detail": str(exc)}
    return {"ok": True, "watchlist_id": wid, "symbol": symbol.upper()}


def add_symbols(
    symbols: str,
    list_id: int | None = None,
    new_list_name: str | None = None,
) -> dict[str, Any]:
    """Bulk-add several symbols to a list in one call. WRITE.

    Args:
        symbols: Whitespace/comma/semicolon-separated tickers, e.g.
            "AAPL, MSFT NVDA; AMD".
        list_id: Existing list to add to. Ignored if new_list_name is given.
        new_list_name: Create a new list with this name and add the symbols to it.

    Returns:
        {"ok": True, "added": [...], "invalid": [...], "list_id": N}.
    """
    result = _add_symbols(symbols, list_id=list_id, new_list_name=new_list_name)
    out = jsonable(result)
    out["ok"] = True
    return out


def remove_from_watchlist(watchlist_id: int) -> dict[str, Any]:
    """Remove a watchlist item by its id. WRITE.

    Returns:
        {"ok": True, "removed_watchlist_id"}.
    """
    _remove(watchlist_id)
    return {"ok": True, "removed_watchlist_id": watchlist_id}


def create_watchlist(name: str) -> dict[str, Any]:
    """Create a new named watchlist list. WRITE.

    Returns:
        {"ok": True, "list_id", "name"} or {"error": ...} on a name collision.
    """
    try:
        lid = _create_list(name)
    except ValueError as exc:
        return {"error": "invalid_request", "detail": str(exc)}
    return {"ok": True, "list_id": lid, "name": name}


def rename_watchlist(list_id: int, name: str) -> dict[str, Any]:
    """Rename a watchlist list. WRITE.

    Returns:
        {"ok": True, "list_id", "name"} or {"error": ...}.
    """
    try:
        _rename_list(list_id, name)
    except ValueError as exc:
        return {"error": "invalid_request", "detail": str(exc)}
    return {"ok": True, "list_id": list_id, "name": name}


def delete_watchlist(list_id: int) -> dict[str, Any]:
    """Delete a watchlist list (its symbol memberships go with it). WRITE.

    Returns:
        {"ok": True, "deleted_list_id"}.
    """
    _delete_list(list_id)
    return {"ok": True, "deleted_list_id": list_id}


def set_target(
    watchlist_id: int,
    target_price: float | None = None,
    target_direction: str | None = None,
) -> dict[str, Any]:
    """Set or clear a price-alert target on a watchlist item (re-arms it). WRITE.

    Args:
        watchlist_id: The item id (from list_watchlist).
        target_price: Target price, or None to clear.
        target_direction: "above" or "below" (required with target_price).

    Returns:
        {"ok": True, "watchlist_id"} or {"error": ...}.
    """
    tp: Decimal | None = None
    if target_price is not None:
        try:
            tp = Decimal(str(target_price))
        except (InvalidOperation, ValueError):
            return {"error": "invalid_target_price", "target_price": target_price}
    try:
        _set_target(watchlist_id, tp, target_direction)  # type: ignore[arg-type]
    except ValueError as exc:
        return {"error": "invalid_request", "detail": str(exc)}
    return {"ok": True, "watchlist_id": watchlist_id}


def toggle_alert(watchlist_id: int, enabled: bool) -> dict[str, Any]:
    """Enable or disable the price alert on a watchlist item. WRITE.

    Returns:
        {"ok": True, "watchlist_id", "alert_enabled"}.
    """
    _toggle_alert(watchlist_id, enabled)
    return {"ok": True, "watchlist_id": watchlist_id, "alert_enabled": enabled}
