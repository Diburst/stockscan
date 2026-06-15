"""Watchlist CRUD + latest-bar enrichment."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.orm import Session

from stockscan.db import session_scope


TargetDirection = Literal["above", "below"]
_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")

# The list every symbol lands in when no list is specified, and the list the
# pre-multi-list mega-watchlist was folded into by migration 0020.
DEFAULT_LIST_NAME = "Watchlist"
_MAX_LIST_NAME = 60


def _normalize_list_name(raw: str) -> str:
    name = raw.strip()
    if not name:
        raise ValueError("list name cannot be empty")
    if len(name) > _MAX_LIST_NAME:
        raise ValueError(f"list name too long (max {_MAX_LIST_NAME} chars)")
    return name


def _normalize_symbol(raw: str) -> str:
    """Uppercase + strip; reject anything that doesn't look like a US ticker."""
    sym = raw.strip().upper()
    if not _SYMBOL_RE.match(sym):
        raise ValueError(f"Invalid symbol: {raw!r}")
    return sym


@dataclass(frozen=True, slots=True)
class WatchlistItem:
    watchlist_id: int
    symbol: str
    target_price: Decimal | None
    target_direction: TargetDirection | None
    alert_enabled: bool
    last_alerted_at: datetime | None
    last_triggered_price: Decimal | None
    note: str | None
    created_at: datetime

    # ---- enrichment from latest bars (populated by list_watchlist) ----
    last_close: Decimal | None = None
    last_volume: int | None = None
    last_bar_date: datetime | None = None
    prev_close: Decimal | None = None

    @property
    def pct_change_today(self) -> float | None:
        if self.last_close is None or self.prev_close is None or self.prev_close == 0:
            return None
        return float((self.last_close - self.prev_close) / self.prev_close)

    @property
    def target_satisfied(self) -> bool:
        """Has the target been crossed by the latest close?"""
        if (
            self.target_price is None
            or self.target_direction is None
            or self.last_close is None
        ):
            return False
        if self.target_direction == "above":
            return self.last_close >= self.target_price
        return self.last_close <= self.target_price


@dataclass(frozen=True, slots=True)
class Watchlist:
    """A named list. ``count`` is the number of symbols on it (populated by
    :func:`list_watchlists`; 0 otherwise)."""

    list_id: int
    name: str
    count: int = 0


# ---------------------------------------------------------------------
# List management
# ---------------------------------------------------------------------
def list_watchlists(*, session: Session | None = None) -> list[Watchlist]:
    """All named lists, alphabetical, each with its symbol count."""
    sql = text(
        """
        SELECT wl.list_id, wl.name, COUNT(m.watchlist_id) AS cnt
        FROM watchlists wl
        LEFT JOIN watchlist_membership m ON m.list_id = wl.list_id
        GROUP BY wl.list_id, wl.name
        ORDER BY wl.name;
        """
    )

    def _run(s: Session) -> list[Watchlist]:
        return [
            Watchlist(list_id=int(r.list_id), name=r.name, count=int(r.cnt))
            for r in s.execute(sql)
        ]

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)


def create_watchlist(name: str, *, session: Session | None = None) -> int:
    """Create (or fetch, if it already exists) a named list. Returns list_id.

    Idempotent on name so "+ new list" with an existing name just selects it.
    """
    nm = _normalize_list_name(name)
    sql = text(
        """
        INSERT INTO watchlists (name) VALUES (:nm)
        ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name
        RETURNING list_id;
        """
    )

    def _run(s: Session) -> int:
        return int(s.execute(sql, {"nm": nm}).scalar_one())

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)


def _resolve_default_list_id(s: Session) -> int:
    """list_id of the default list, creating it if a fresh DB lacks it."""
    return create_watchlist(DEFAULT_LIST_NAME, session=s)


def resolve_selection(
    raw: str | None, *, session: Session | None = None
) -> tuple[int | None, str]:
    """Map a ``?list=`` query value to ``(list_id_or_None, display_name)``.

    ``"all"`` ⇒ ``(None, "All")`` (every symbol across all lists). A blank or
    unknown value falls back to the default list, creating it if a fresh DB
    somehow lacks it, so the pages always have a valid selection.
    """

    def _run(s: Session) -> tuple[int | None, str]:
        if raw is not None and raw.strip().lower() == "all":
            return None, "All"
        lists = list_watchlists(session=s)
        by_id = {wl.list_id: wl.name for wl in lists}
        if raw and raw.strip():
            try:
                lid = int(raw)
            except ValueError:
                lid = None
            if lid in by_id:
                return lid, by_id[lid]
        for wl in lists:
            if wl.name == DEFAULT_LIST_NAME:
                return wl.list_id, wl.name
        return create_watchlist(DEFAULT_LIST_NAME, session=s), DEFAULT_LIST_NAME

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)


def delete_watchlist(list_id: int, *, session: Session | None = None) -> None:
    """Delete a list. Memberships cascade; any symbol left on no list at all
    is removed entirely (it would otherwise be unreachable)."""
    del_list = text("DELETE FROM watchlists WHERE list_id = :lid")
    cleanup = text(
        """
        DELETE FROM watchlist_items wi
        WHERE NOT EXISTS (
            SELECT 1 FROM watchlist_membership m WHERE m.watchlist_id = wi.watchlist_id
        );
        """
    )

    def _run(s: Session) -> None:
        s.execute(del_list, {"lid": list_id})
        s.execute(cleanup)

    if session is not None:
        _run(session)
        return
    with session_scope() as s:
        _run(s)


# ---------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------
def add_to_watchlist(
    symbol: str,
    *,
    target_price: Decimal | None = None,
    target_direction: TargetDirection | None = None,
    note: str | None = None,
    list_id: int | None = None,
    new_list_name: str | None = None,
    session: Session | None = None,
) -> int:
    """Insert a watchlist symbol and add it to a list. Returns watchlist_id.

    The symbol lands on a list resolved in this order:

      1. ``new_list_name`` (created if needed) — the "+ new list" path,
      2. ``list_id`` — an existing list chosen in the UI,
      3. the default list (``DEFAULT_LIST_NAME``) when neither is given.

    Raises ValueError on invalid symbol, target, or list name.
    Idempotent on (symbol): re-adding an existing symbol keeps its target/
    alert/note and just ensures the membership row. A symbol can belong to
    several lists at once.
    """
    sym = _normalize_symbol(symbol)
    if (target_price is None) != (target_direction is None):
        raise ValueError("target_price and target_direction must be set together")
    if target_price is not None and target_price <= 0:
        raise ValueError("target_price must be positive")
    if target_direction is not None and target_direction not in {"above", "below"}:
        raise ValueError("target_direction must be 'above' or 'below'")

    item_sql = text(
        """
        INSERT INTO watchlist_items (symbol, target_price, target_direction, note)
        VALUES (:sym, :tp, :td, :note)
        ON CONFLICT (symbol) DO UPDATE SET symbol = EXCLUDED.symbol
        RETURNING watchlist_id;
        """
    )
    member_sql = text(
        """
        INSERT INTO watchlist_membership (list_id, watchlist_id)
        VALUES (:lid, :wid)
        ON CONFLICT DO NOTHING;
        """
    )

    def _run(s: Session) -> int:
        if new_list_name and new_list_name.strip():
            lid = create_watchlist(new_list_name, session=s)
        elif list_id is not None:
            lid = list_id
        else:
            lid = _resolve_default_list_id(s)
        wid = int(
            s.execute(
                item_sql,
                {"sym": sym, "tp": target_price, "td": target_direction, "note": note},
            ).scalar_one()
        )
        s.execute(member_sql, {"lid": lid, "wid": wid})
        return wid

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)


@dataclass(frozen=True, slots=True)
class AddSymbolsResult:
    """Outcome of a bulk add. ``added`` are the symbols now on the list
    (newly inserted or already present); ``invalid`` failed validation."""

    added: list[str]
    invalid: list[str]
    list_id: int


_SPLIT_RE = re.compile(r"[\s,;]+")


def add_symbols(
    raw: str,
    *,
    list_id: int | None = None,
    new_list_name: str | None = None,
    session: Session | None = None,
) -> AddSymbolsResult:
    """Bulk-add a free-form block of tickers to a list.

    Accepts commas, whitespace, semicolons, or newlines between symbols — so
    a pasted ``AAPL, MSFT  GOOG;TSLA`` all works. Each symbol is validated and
    upserted (idempotent), then linked to the resolved list. Does NOT backfill
    price history — that's the caller's concern (the watchlist route defers it
    to the next Refresh). Invalid tokens are collected, not raised, so one bad
    ticker doesn't abort the batch.
    """
    tokens = [t for t in _SPLIT_RE.split(raw or "") if t]
    added: list[str] = []
    invalid: list[str] = []
    seen: set[str] = set()

    item_sql = text(
        """
        INSERT INTO watchlist_items (symbol) VALUES (:sym)
        ON CONFLICT (symbol) DO UPDATE SET symbol = EXCLUDED.symbol
        RETURNING watchlist_id;
        """
    )
    member_sql = text(
        """
        INSERT INTO watchlist_membership (list_id, watchlist_id)
        VALUES (:lid, :wid)
        ON CONFLICT DO NOTHING;
        """
    )

    def _run(s: Session) -> AddSymbolsResult:
        if new_list_name and new_list_name.strip():
            lid = create_watchlist(new_list_name, session=s)
        elif list_id is not None:
            lid = list_id
        else:
            lid = _resolve_default_list_id(s)
        for tok in tokens:
            try:
                sym = _normalize_symbol(tok)
            except ValueError:
                invalid.append(tok)
                continue
            if sym in seen:
                continue
            seen.add(sym)
            wid = int(s.execute(item_sql, {"sym": sym}).scalar_one())
            s.execute(member_sql, {"lid": lid, "wid": wid})
            added.append(sym)
        return AddSymbolsResult(added=added, invalid=invalid, list_id=lid)

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)


def rename_watchlist(list_id: int, name: str, *, session: Session | None = None) -> None:
    """Rename a list. Raises ValueError on empty/too-long names or a name
    collision with a different list."""
    nm = _normalize_list_name(name)
    collide = text("SELECT list_id FROM watchlists WHERE name = :nm")
    update = text("UPDATE watchlists SET name = :nm WHERE list_id = :lid")

    def _run(s: Session) -> None:
        existing = s.execute(collide, {"nm": nm}).scalar()
        if existing is not None and int(existing) != list_id:
            raise ValueError(f"a list named {nm!r} already exists")
        s.execute(update, {"nm": nm, "lid": list_id})

    if session is not None:
        _run(session)
        return
    with session_scope() as s:
        _run(s)


def remove_from_list(
    watchlist_id: int, list_id: int, *, session: Session | None = None
) -> None:
    """Remove a symbol from one list. If that was its last list, the symbol
    (and its target/alert/note) is removed entirely."""
    drop = text(
        "DELETE FROM watchlist_membership WHERE watchlist_id = :wid AND list_id = :lid"
    )
    orphan = text(
        """
        DELETE FROM watchlist_items wi
        WHERE wi.watchlist_id = :wid
          AND NOT EXISTS (
              SELECT 1 FROM watchlist_membership m WHERE m.watchlist_id = :wid
          );
        """
    )

    def _run(s: Session) -> None:
        s.execute(drop, {"wid": watchlist_id, "lid": list_id})
        s.execute(orphan, {"wid": watchlist_id})

    if session is not None:
        _run(session)
        return
    with session_scope() as s:
        _run(s)


def remove_from_watchlist(watchlist_id: int, *, session: Session | None = None) -> None:
    sql = text("DELETE FROM watchlist_items WHERE watchlist_id = :wid")

    def _run(s: Session) -> None:
        s.execute(sql, {"wid": watchlist_id})

    if session is not None:
        _run(session)
        return
    with session_scope() as s:
        _run(s)


def remove_symbol(symbol: str, *, session: Session | None = None) -> int:
    """Remove every watchlist entry for ``symbol`` (across all lists).

    Backs the Dashboard pill's click-to-unwatch toggle, which knows the
    symbol but not the watchlist_id. Returns the number of rows removed
    so the caller can distinguish "removed" from "wasn't watched".
    """
    sql = text("DELETE FROM watchlist_items WHERE UPPER(symbol) = UPPER(:sym)")

    def _run(s: Session) -> int:
        return s.execute(sql, {"sym": symbol.strip()}).rowcount or 0

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)


def set_target(
    watchlist_id: int,
    target_price: Decimal | None,
    target_direction: TargetDirection | None,
    *,
    session: Session | None = None,
) -> None:
    """Update or clear the price target. Re-arms the alert."""
    if (target_price is None) != (target_direction is None):
        raise ValueError("target_price and target_direction must be set together")
    if target_price is not None and target_price <= 0:
        raise ValueError("target_price must be positive")

    sql = text(
        """
        UPDATE watchlist_items
        SET target_price = :tp,
            target_direction = :td,
            alert_enabled = TRUE,
            last_alerted_at = NULL,
            last_triggered_price = NULL
        WHERE watchlist_id = :wid;
        """
    )

    def _run(s: Session) -> None:
        s.execute(sql, {"wid": watchlist_id, "tp": target_price, "td": target_direction})

    if session is not None:
        _run(session)
        return
    with session_scope() as s:
        _run(s)


def toggle_alert(
    watchlist_id: int, enabled: bool, *, session: Session | None = None
) -> None:
    sql = text(
        "UPDATE watchlist_items SET alert_enabled = :e WHERE watchlist_id = :wid"
    )

    def _run(s: Session) -> None:
        s.execute(sql, {"wid": watchlist_id, "e": enabled})

    if session is not None:
        _run(session)
        return
    with session_scope() as s:
        _run(s)


def mark_alerted(
    watchlist_id: int, triggered_price: Decimal, *, session: Session | None = None
) -> None:
    """Record an alert firing and disable until manually re-enabled."""
    sql = text(
        """
        UPDATE watchlist_items
        SET last_alerted_at = NOW(),
            last_triggered_price = :p,
            alert_enabled = FALSE
        WHERE watchlist_id = :wid;
        """
    )

    def _run(s: Session) -> None:
        s.execute(sql, {"wid": watchlist_id, "p": triggered_price})

    if session is not None:
        _run(session)
        return
    with session_scope() as s:
        _run(s)


# ---------------------------------------------------------------------
# Queries (with latest-bar enrichment)
# ---------------------------------------------------------------------
_LIST_SQL = text(
    """
    WITH latest AS (
        SELECT symbol, close AS last_close, volume AS last_volume, bar_ts AS last_bar_ts,
               row_number() OVER (PARTITION BY symbol ORDER BY bar_ts DESC) AS rn
        FROM bars
        WHERE interval = '1d'
    ),
    prev AS (
        SELECT symbol, close AS prev_close,
               row_number() OVER (PARTITION BY symbol ORDER BY bar_ts DESC) AS rn
        FROM bars
        WHERE interval = '1d'
    )
    SELECT
        w.watchlist_id, w.symbol, w.target_price, w.target_direction,
        w.alert_enabled, w.last_alerted_at, w.last_triggered_price,
        w.note, w.created_at,
        l.last_close, l.last_volume, l.last_bar_ts,
        p.prev_close
    FROM watchlist_items w
    LEFT JOIN latest l ON l.symbol = w.symbol AND l.rn = 1
    LEFT JOIN prev   p ON p.symbol = w.symbol AND p.rn = 2
    WHERE (
        CAST(:lid AS BIGINT) IS NULL
        OR EXISTS (
            SELECT 1 FROM watchlist_membership m
            WHERE m.watchlist_id = w.watchlist_id AND m.list_id = CAST(:lid AS BIGINT)
        )
    )
    ORDER BY w.symbol;
    """
)


def list_watchlist(
    *, list_id: int | None = None, session: Session | None = None
) -> list[WatchlistItem]:
    """Watchlist items, optionally filtered to one list.

    ``list_id=None`` returns every item across all lists (the "All" view);
    a list_id restricts to that list's members. The EXISTS filter keeps one
    row per symbol either way (no membership-join fan-out)."""
    def _row(r: Any) -> WatchlistItem:
        def _dec(v: Any) -> Decimal | None:
            return Decimal(str(v)) if v is not None else None

        return WatchlistItem(
            watchlist_id=int(r.watchlist_id),
            symbol=r.symbol,
            target_price=_dec(r.target_price),
            target_direction=r.target_direction,
            alert_enabled=bool(r.alert_enabled),
            last_alerted_at=r.last_alerted_at,
            last_triggered_price=_dec(r.last_triggered_price),
            note=r.note,
            created_at=r.created_at,
            last_close=_dec(r.last_close),
            last_volume=int(r.last_volume) if r.last_volume is not None else None,
            last_bar_date=r.last_bar_ts,
            prev_close=_dec(r.prev_close),
        )

    def _run(s: Session) -> list[WatchlistItem]:
        return [_row(r) for r in s.execute(_LIST_SQL, {"lid": list_id})]

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)


def get_triggered(*, session: Session | None = None) -> list[WatchlistItem]:
    """Items that should fire an alert: target crossed AND alert_enabled."""
    items = list_watchlist(session=session)
    return [it for it in items if it.alert_enabled and it.target_satisfied]


def watchlist_symbols(
    *, list_id: int | None = None, session: Session | None = None
) -> set[str]:
    """Lightweight: just the set of symbols currently on the watchlist.

    Used by pages that need to render "watching" badges next to symbols
    without paying for the full bar-join enrichment. ``list_id`` restricts
    to one list; ``None`` is every symbol across all lists.
    """
    sql = text(
        """
        SELECT w.symbol FROM watchlist_items w
        WHERE (
            CAST(:lid AS BIGINT) IS NULL
            OR EXISTS (
                SELECT 1 FROM watchlist_membership m
                WHERE m.watchlist_id = w.watchlist_id AND m.list_id = CAST(:lid AS BIGINT)
            )
        );
        """
    )

    def _run(s: Session) -> set[str]:
        return {row[0] for row in s.execute(sql, {"lid": list_id}).all()}

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)
