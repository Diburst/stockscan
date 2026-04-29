"""Persistence layer for the macro_series table.

Mirrors :mod:`stockscan.data.store` (bars) for scalar daily-frequency
macro time series — HY OAS today, plus headroom for yield-curve spreads,
DXY level, etc. as we add more cross-asset signals.

Invariants:
  - Every fetch goes through ``upsert_macro_series``; never bypass.
  - Upsert is idempotent on the (series_code, as_of_date) primary key.
  - The DB is the source of truth; the FRED provider is just a refresh source.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pandas as pd
from sqlalchemy import text

from stockscan.db import session_scope

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import date

    from sqlalchemy.orm import Session

    from stockscan.data.providers.base import MacroRow

_UPSERT_SQL = text(
    """
    INSERT INTO macro_series
        (series_code, as_of_date, value, source, fetched_at)
    VALUES
        (:series_code, :as_of_date, :value, :source, NOW())
    ON CONFLICT (series_code, as_of_date) DO UPDATE SET
        value      = EXCLUDED.value,
        source     = EXCLUDED.source,
        fetched_at = NOW();
    """
)

_GET_RANGE_SQL = text(
    """
    SELECT as_of_date, value
    FROM macro_series
    WHERE series_code = :series_code
      AND as_of_date BETWEEN :start AND :end
    ORDER BY as_of_date
    """
)

_LATEST_SQL = text(
    """
    SELECT value, as_of_date
    FROM macro_series
    WHERE series_code = :series_code
      AND as_of_date <= :as_of
    ORDER BY as_of_date DESC
    LIMIT 1
    """
)


def upsert_macro_series(
    rows: Iterable[MacroRow],
    *,
    session: Session | None = None,
) -> int:
    """Bulk upsert macro series observations. Returns the row count.

    Empty input is a no-op. The upsert is idempotent on
    (series_code, as_of_date) so partial-day re-fetches are safe.
    """
    payload: list[dict[str, Any]] = [
        {
            "series_code": r.series_code,
            "as_of_date": r.as_of_date,
            "value": r.value,
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


def get_macro_series(
    series_code: str,
    start: date,
    end: date,
    *,
    session: Session | None = None,
) -> pd.Series:
    """Return a date-indexed ``pd.Series`` for one series in [start, end].

    The Series is named after the series code so callers can ``concat``
    multiple series side-by-side without column-name collisions. Values
    are floats — the regime detector wants pandas-native math, and the
    NUMERIC(18,6) precision is more than enough headroom for HY OAS.
    Empty result returns an empty Series of dtype float.
    """

    def _run(s: Session) -> pd.Series:
        result = s.execute(
            _GET_RANGE_SQL,
            {"series_code": series_code, "start": start, "end": end},
        )
        rows = result.mappings().all()
        if not rows:
            return pd.Series(name=series_code, dtype=float)
        df = pd.DataFrame(rows)
        df["as_of_date"] = pd.to_datetime(df["as_of_date"])
        series = df.set_index("as_of_date")["value"].astype(float)
        series.name = series_code
        return series

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)


def latest_macro_value(
    series_code: str,
    as_of: date,
    *,
    session: Session | None = None,
) -> Decimal | None:
    """Return the most recent value at-or-before ``as_of``, or None.

    Useful for "what's the most-recent HY OAS print as of this scan
    date?" — common on weekends/holidays where today's observation
    doesn't exist yet.
    """

    def _run(s: Session) -> Decimal | None:
        row = s.execute(_LATEST_SQL, {"series_code": series_code, "as_of": as_of}).first()
        if row is None:
            return None
        return Decimal(str(row.value))

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)
