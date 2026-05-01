"""Bar persistence layer — idempotent upsert into the bars hypertable.

DESIGN §4.1 invariants:
  - Every fetch goes through `upsert_bars`; never bypass.
  - Upsert is idempotent on the (symbol, interval, bar_ts) primary key.
  - The DB is the source of truth; the provider is just a refresh source.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

import pandas as pd
from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from stockscan.data.providers.base import BarRow
from stockscan.db import session_scope


_UPSERT_SQL = text(
    """
    INSERT INTO bars
        (symbol, bar_ts, interval, open, high, low, close, adj_close, volume, source, fetched_at)
    VALUES
        (:symbol, :bar_ts, :interval, :open, :high, :low, :close, :adj_close, :volume, :source, NOW())
    ON CONFLICT (symbol, interval, bar_ts) DO UPDATE SET
        open       = EXCLUDED.open,
        high       = EXCLUDED.high,
        low        = EXCLUDED.low,
        close      = EXCLUDED.close,
        adj_close  = EXCLUDED.adj_close,
        volume     = EXCLUDED.volume,
        source     = EXCLUDED.source,
        fetched_at = NOW();
    """
)


def upsert_bars(rows: Iterable[BarRow], *, session: Session | None = None) -> int:
    """Upsert bars idempotently. Returns the row count."""
    payload: list[dict[str, Any]] = [
        {
            "symbol": r.symbol,
            "bar_ts": r.bar_ts,
            "interval": r.interval,
            "open": r.open,
            "high": r.high,
            "low": r.low,
            "close": r.close,
            "adj_close": r.adj_close,
            "volume": r.volume,
            "source": r.source,
        }
        for r in rows
    ]
    if not payload:
        return 0

    if session is not None:
        session.execute(_UPSERT_SQL, payload)
        return len(payload)

    with session_scope() as s:
        s.execute(_UPSERT_SQL, payload)
    return len(payload)


def get_bars(
    symbol: str,
    start: date | datetime,
    end: date | datetime,
    interval: str = "1d",
    *,
    session: Session | None = None,
) -> pd.DataFrame:
    """Return bars as a DataFrame indexed by `bar_ts` (UTC)."""
    if isinstance(start, date) and not isinstance(start, datetime):
        start_dt = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
    else:
        start_dt = start  # type: ignore[assignment]
    if isinstance(end, date) and not isinstance(end, datetime):
        end_dt = datetime(end.year, end.month, end.day, 23, 59, 59, tzinfo=timezone.utc)
    else:
        end_dt = end  # type: ignore[assignment]

    sql = text(
        """
        SELECT symbol, bar_ts, interval, open, high, low, close, adj_close, volume, source
        FROM bars
        WHERE symbol = :symbol
          AND interval = :interval
          AND bar_ts BETWEEN :start AND :end
        ORDER BY bar_ts
        """
    )

    def _run(s: Session) -> pd.DataFrame:
        result = s.execute(
            sql,
            {"symbol": symbol, "interval": interval, "start": start_dt, "end": end_dt},
        )
        df = pd.DataFrame(result.mappings().all())
        if not df.empty:
            df["bar_ts"] = pd.to_datetime(df["bar_ts"], utc=True)
            df = df.set_index("bar_ts")
            for col in ("open", "high", "low", "close", "adj_close"):
                df[col] = df[col].astype(float)
        return df

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)


def latest_bar_date(symbol: str, *, session: Session | None = None) -> date | None:
    """Return the date of the most recent stored bar for `symbol`, or None.

    Uses positional row access (``row[0]``) rather than attribute access on
    the aliased ``AS d`` column — SQLAlchemy 2.x's Row attribute lookup
    behaves inconsistently with short/aggregate aliases (sometimes returns
    the Row itself, breaking downstream f-string formatting).
    """
    sql = text("SELECT MAX(bar_ts)::date FROM bars WHERE symbol = :symbol AND interval='1d'")

    def _run(s: Session) -> date | None:
        row = s.execute(sql, {"symbol": symbol}).first()
        if row is None:
            return None
        val = row[0]
        return val if val is not None else None

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)


def upsert_bars_from_df(df: pd.DataFrame, symbol: str, source: str = "manual") -> int:
    """Convenience: upsert bars from a DataFrame (e.g., from yfinance fixtures)."""
    rows: list[BarRow] = []
    for ts, r in df.iterrows():
        if not isinstance(ts, pd.Timestamp):
            ts = pd.Timestamp(ts)
        rows.append(
            BarRow(
                symbol=symbol,
                bar_ts=ts.to_pydatetime().astimezone(timezone.utc),
                interval="1d",
                open=Decimal(str(r["open"])),
                high=Decimal(str(r["high"])),
                low=Decimal(str(r["low"])),
                close=Decimal(str(r["close"])),
                adj_close=Decimal(str(r.get("adj_close", r["close"]))),
                volume=int(r["volume"]),
                source=source,
            )
        )
    return upsert_bars(rows)
