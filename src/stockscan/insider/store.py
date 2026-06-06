"""Persistence + queries for the ``insider_transactions`` table."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date as _date
from typing import TYPE_CHECKING, Any

from sqlalchemy import text

from stockscan.db import session_scope

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


@dataclass(frozen=True, slots=True)
class InsiderTransaction:
    """One row from ``insider_transactions``."""

    transaction_id: int
    symbol: str
    transaction_date: _date
    filed_date: _date | None
    insider_name: str | None
    insider_title: str | None
    transaction_code: str  # 'P' | 'S'
    shares: float | None
    price: float | None
    value: float | None
    shares_owned_after: float | None

    @property
    def is_buy(self) -> bool:
        return self.transaction_code == "P"


def _to_date(v: Any) -> _date | None:
    if not v:
        return None
    if isinstance(v, _date):
        return v
    try:
        return _date.fromisoformat(str(v)[:10])
    except (ValueError, TypeError):
        return None


def _to_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


_UPSERT_SQL = text(
    """
    INSERT INTO insider_transactions (
        symbol, transaction_date, filed_date, insider_name, insider_title,
        transaction_code, shares, price, value, shares_owned_after, raw
    ) VALUES (
        :symbol, :transaction_date, :filed_date, :insider_name, :insider_title,
        :transaction_code, :shares, :price, :value, :shares_owned_after,
        CAST(:raw AS JSONB)
    )
    ON CONFLICT (symbol, transaction_date, insider_name, transaction_code, shares)
    DO UPDATE SET
        filed_date         = COALESCE(EXCLUDED.filed_date, insider_transactions.filed_date),
        insider_title      = COALESCE(EXCLUDED.insider_title, insider_transactions.insider_title),
        price              = COALESCE(EXCLUDED.price, insider_transactions.price),
        value              = COALESCE(EXCLUDED.value, insider_transactions.value),
        shares_owned_after = COALESCE(EXCLUDED.shares_owned_after, insider_transactions.shares_owned_after),
        raw                = EXCLUDED.raw
    """
)


def upsert_transactions(
    records: list[dict[str, Any]],
    *,
    symbol: str | None = None,
    session: Session | None = None,
) -> int:
    """Upsert raw EODHD insider-transaction records. Returns rows touched.

    The ``symbol`` kwarg is a **fallback** for when the upstream payload
    doesn't include a per-record ticker (common when the API was queried
    with the ``code=`` parameter — EODHD doesn't always echo the symbol
    back into every record). ``_pull_for_symbol`` passes it explicitly;
    bulk pulls without a symbol filter rely on each record's own ``code``.

    Records without a recognised transaction code (P or S), a transaction
    date, or any way to derive a symbol are skipped. EODHD has historically
    returned only P/S, but other Form-4 codes (G for gift, A for grant,
    etc.) are silently filtered out to keep the buy/sell aggregates clean.
    """
    if not records:
        return 0

    fallback_symbol = (symbol or "").split(".")[0].strip().upper() or None

    payload: list[dict[str, Any]] = []
    for r in records:
        # Transaction code (P/S). EODHD historically uses ``transactionCode``
        # but defensively try alternates too.
        code = (
            r.get("transactionCode")
            or r.get("transaction_code")
            or ""
        )
        code = str(code).upper()
        if code not in {"P", "S"}:
            continue

        # Symbol detection: prefer the per-record ticker (with EODHD
        # exchange suffix stripped); fall back to the caller-supplied one
        # so we never silently drop records when the API omits the code
        # field on a per-symbol pull.
        record_symbol = (
            r.get("code")
            or r.get("symbol")
            or r.get("ticker")
            or ""
        )
        record_symbol = str(record_symbol).split(".")[0].strip().upper()
        sym = record_symbol or fallback_symbol
        if not sym:
            continue

        tdate = _to_date(
            r.get("transactionDate")
            or r.get("transaction_date")
            or r.get("date")
        )
        if tdate is None:
            continue

        shares = _to_float(
            r.get("transactionAmount")
            or r.get("transaction_amount")
            or r.get("shares")
        )
        price = _to_float(
            r.get("transactionPrice")
            or r.get("transaction_price")
            or r.get("price")
        )
        value: float | None
        if shares is not None and price is not None:
            value = shares * price
        else:
            value = _to_float(
                r.get("ownership_change_value") or r.get("transactionValue")
            )
        payload.append({
            "symbol": sym,
            "transaction_date": tdate,
            "filed_date": _to_date(
                r.get("reportDate")
                or r.get("report_date")
                or r.get("filed_date")
            ),
            "insider_name": (
                r.get("ownerName")
                or r.get("owner_name")
                or r.get("name")
            ),
            "insider_title": (
                r.get("ownerTitle")
                or r.get("ownerRelationship")
                or r.get("owner_relationship")
                or r.get("title")
            ),
            "transaction_code": code,
            "shares": shares,
            "price": price,
            "value": value,
            "shares_owned_after": _to_float(
                r.get("postTransactionAmount")
                or r.get("post_transaction_amount")
                or r.get("shares_owned_after")
            ),
            "raw": json.dumps(r),
        })

    if not payload:
        return 0

    def _run(s: Session) -> int:
        result = s.execute(_UPSERT_SQL, payload)
        return result.rowcount or len(payload)

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)


_RECENT_SQL = text(
    """
    SELECT transaction_id, symbol, transaction_date, filed_date,
           insider_name, insider_title, transaction_code,
           shares, price, value, shares_owned_after
    FROM insider_transactions
    WHERE symbol = :symbol
      AND transaction_date >= :since
    ORDER BY transaction_date DESC, value DESC NULLS LAST
    LIMIT :limit
    """
)


def recent_transactions(
    symbol: str,
    *,
    lookback_days: int = 90,
    limit: int = 20,
    session: Session | None = None,
) -> list[InsiderTransaction]:
    """Last N insider transactions for ``symbol`` within the lookback window."""
    from datetime import date, timedelta
    since = date.today() - timedelta(days=lookback_days)

    def _run(s: Session) -> list[InsiderTransaction]:
        rows = s.execute(
            _RECENT_SQL,
            {"symbol": symbol, "since": since, "limit": limit},
        ).all()
        return [
            InsiderTransaction(
                transaction_id=int(r.transaction_id),
                symbol=r.symbol,
                transaction_date=r.transaction_date,
                filed_date=r.filed_date,
                insider_name=r.insider_name,
                insider_title=r.insider_title,
                transaction_code=r.transaction_code,
                shares=float(r.shares) if r.shares is not None else None,
                price=float(r.price) if r.price is not None else None,
                value=float(r.value) if r.value is not None else None,
                shares_owned_after=(
                    float(r.shares_owned_after)
                    if r.shares_owned_after is not None
                    else None
                ),
            )
            for r in rows
        ]

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)


_NET_BUYS_SQL = text(
    """
    SELECT
        COALESCE(SUM(CASE WHEN transaction_code = 'P' THEN 1 ELSE 0 END), 0) AS buy_count,
        COALESCE(SUM(CASE WHEN transaction_code = 'S' THEN 1 ELSE 0 END), 0) AS sell_count,
        COALESCE(SUM(CASE WHEN transaction_code = 'P' THEN value ELSE 0 END), 0) AS buy_value,
        COALESCE(SUM(CASE WHEN transaction_code = 'S' THEN value ELSE 0 END), 0) AS sell_value
    FROM insider_transactions
    WHERE symbol = :symbol
      AND transaction_date >= :since
    """
)


@dataclass(frozen=True, slots=True)
class InsiderNetBuys:
    buy_count: int
    sell_count: int
    buy_value: float  # dollar volume of all P transactions
    sell_value: float

    @property
    def net_count(self) -> int:
        """Open-market buys minus sales over the window."""
        return self.buy_count - self.sell_count

    @property
    def net_value(self) -> float:
        return self.buy_value - self.sell_value

    @property
    def has_activity(self) -> bool:
        return (self.buy_count + self.sell_count) > 0


def net_buys_90d(
    symbol: str,
    *,
    lookback_days: int = 90,
    session: Session | None = None,
) -> InsiderNetBuys:
    """Aggregated trailing-N-day insider activity for ``symbol``.

    The watchlist pill renders ``net_count`` (color-coded by sign).
    The analysis card uses the full breakdown.
    """
    from datetime import date, timedelta
    since = date.today() - timedelta(days=lookback_days)

    def _run(s: Session) -> InsiderNetBuys:
        row = s.execute(_NET_BUYS_SQL, {"symbol": symbol, "since": since}).first()
        if row is None:
            return InsiderNetBuys(0, 0, 0.0, 0.0)
        return InsiderNetBuys(
            buy_count=int(row.buy_count or 0),
            sell_count=int(row.sell_count or 0),
            buy_value=float(row.buy_value or 0.0),
            sell_value=float(row.sell_value or 0.0),
        )

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)
