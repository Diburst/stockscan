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


# ---------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------
def add_to_watchlist(
    symbol: str,
    *,
    target_price: Decimal | None = None,
    target_direction: TargetDirection | None = None,
    note: str | None = None,
    session: Session | None = None,
) -> int:
    """Insert a new watchlist row. Returns watchlist_id.

    Raises ValueError on invalid symbol or target.
    Idempotent on (symbol): re-adding an existing symbol is a no-op and
    returns the existing watchlist_id.
    """
    sym = _normalize_symbol(symbol)
    if (target_price is None) != (target_direction is None):
        raise ValueError("target_price and target_direction must be set together")
    if target_price is not None and target_price <= 0:
        raise ValueError("target_price must be positive")
    if target_direction is not None and target_direction not in {"above", "below"}:
        raise ValueError("target_direction must be 'above' or 'below'")

    sql = text(
        """
        INSERT INTO watchlist_items (symbol, target_price, target_direction, note)
        VALUES (:sym, :tp, :td, :note)
        ON CONFLICT (symbol) DO UPDATE SET symbol = EXCLUDED.symbol
        RETURNING watchlist_id;
        """
    )

    def _run(s: Session) -> int:
        return int(
            s.execute(
                sql, {"sym": sym, "tp": target_price, "td": target_direction, "note": note}
            ).scalar_one()
        )

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)


def remove_from_watchlist(watchlist_id: int, *, session: Session | None = None) -> None:
    sql = text("DELETE FROM watchlist_items WHERE watchlist_id = :wid")

    def _run(s: Session) -> None:
        s.execute(sql, {"wid": watchlist_id})

    if session is not None:
        _run(session)
        return
    with session_scope() as s:
        _run(s)


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
    ORDER BY w.created_at;
    """
)


def list_watchlist(*, session: Session | None = None) -> list[WatchlistItem]:
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
        return [_row(r) for r in s.execute(_LIST_SQL)]

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)


def get_triggered(*, session: Session | None = None) -> list[WatchlistItem]:
    """Items that should fire an alert: target crossed AND alert_enabled."""
    items = list_watchlist(session=session)
    return [it for it in items if it.alert_enabled and it.target_satisfied]


def watchlist_symbols(*, session: Session | None = None) -> set[str]:
    """Lightweight: just the set of symbols currently on the watchlist.

    Used by pages that need to render "watching" badges next to symbols
    without paying for the full bar-join enrichment.
    """
    sql = text("SELECT symbol FROM watchlist_items")

    def _run(s: Session) -> set[str]:
        return {row[0] for row in s.execute(sql).all()}

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)
