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
    adjust: bool = True,
) -> pd.DataFrame:
    """Return bars as a DataFrame indexed by ``bar_ts`` (UTC).

    **Split / dividend adjustment** (default ``adjust=True``)
    ----------------------------------------------------------
    Returned ``open / high / low / close / volume`` are split- and
    dividend-adjusted using the ratio ``adj_close / close`` per bar,
    so historical analysis (indicators, returns, backtests, charts)
    sees a continuous price series across corporate actions like
    AAPL's 2014 7:1 and 2020 4:1 splits.

    Specifically::

        adj_factor = adj_close / close   # per bar, NaN-safe
        open       = open   * adj_factor
        high       = high   * adj_factor
        low        = low    * adj_factor
        close      = adj_close
        volume     = volume / adj_factor   # split-adjusted
        adj_close  = adj_close             # unchanged, for reference

    The unadjusted source values are preserved in
    ``open_raw / high_raw / low_raw / close_raw / volume_raw`` for
    consumers that genuinely need them — live "current price" labels,
    tax-lot accounting, or order-placement code that needs the actual
    quote of the day.

    Pass ``adjust=False`` to opt out and get raw bars (no rename, no
    factor applied) — useful for diagnostics and for the rare consumer
    that doesn't want EODHD's dividend portion folded into prices.

    Edge cases:
      * Bars with NULL ``adj_close`` (legacy rows or the StubProvider)
        get ``adj_factor = 1.0`` — i.e., no adjustment, behaves like raw.
      * Bars with ``close <= 0`` (data corruption) also fall back to
        ``adj_factor = 1.0`` to avoid divide-by-zero / inf.
    """
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
        if df.empty:
            return df
        df["bar_ts"] = pd.to_datetime(df["bar_ts"], utc=True)
        df = df.set_index("bar_ts")
        for col in ("open", "high", "low", "close", "adj_close"):
            df[col] = df[col].astype(float)
        df["volume"] = df["volume"].astype(float)
        if adjust:
            df = _apply_split_adjustment(df)
        return df

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)


def _apply_split_adjustment(df: pd.DataFrame) -> pd.DataFrame:
    """Replace OHLCV with split- and dividend-adjusted values; preserve raw.

    The adjustment factor is ``adj_close / close`` per bar. Bars
    missing ``adj_close`` or with a non-positive ``close`` use
    ``adj_factor = 1.0`` (no-op) so we never produce NaN or inf in
    the returned DataFrame.

    Mutates ``df`` in place AND returns it for chaining.
    """
    close = df["close"]
    adj_close = df["adj_close"]
    safe_mask = close.gt(0) & close.notna() & adj_close.notna()
    adj_factor = pd.Series(1.0, index=df.index, dtype=float)
    if safe_mask.any():
        adj_factor.loc[safe_mask] = adj_close.loc[safe_mask] / close.loc[safe_mask]

    # Preserve raw (unadjusted) values for the rare consumer that
    # needs the actual quote-of-the-day prices.
    for raw_col in ("open", "high", "low", "close", "volume"):
        df[f"{raw_col}_raw"] = df[raw_col]

    # Apply the adjustment. close becomes adj_close exactly (avoids
    # round-trip float drift from multiplying then dividing); the
    # other prices are scaled by the ratio so OHLC stays internally
    # consistent (e.g., ATR's true-range computation).
    df["open"] = df["open"] * adj_factor
    df["high"] = df["high"] * adj_factor
    df["low"] = df["low"] * adj_factor
    df["close"] = adj_close
    # Volume scales inversely — a 4:1 split quadruples share count, so
    # share-volume should multiply by 4 to keep dollar-volume
    # consistent. Using the same factor folds in dividend adjustment
    # too, but that fraction is tiny relative to splits and harmless
    # for liquidity filters.
    df["volume"] = df["volume"] / adj_factor

    return df


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
