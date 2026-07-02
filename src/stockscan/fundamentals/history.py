"""Point-in-time shares-outstanding history, reconstructed from the EODHD blob.

``fundamentals_snapshot`` keeps only the *latest* share count. For an accurate
historical cap-weighted composite we need shares *as of each past date*, so that
market cap(t) = shares(t) x price(t) reflects the company's size at that time
rather than its size today. The full ``/fundamentals`` payload we already persist
in ``fundamentals_snapshot.raw_payload`` carries that history — we just never
extracted it. This module does the extraction (a pure function, easy to test)
and the persistence into ``fundamentals_history``.

No new API calls: :func:`extract_shares_history` parses a payload we already have,
and :func:`backfill_shares_history_from_snapshots` replays it over every stored
snapshot. The go-forward path is wired into :func:`stockscan.fundamentals.store.
upsert_fundamentals`, so each routine fundamentals refresh keeps the history
current as a side effect of storing the snapshot.

Extraction precedence (most-granular first):

  1. ``outstandingShares.quarterly`` — the canonical EODHD time series,
  2. ``outstandingShares.annual``   — coarser fallback,
  3. ``Financials.Balance_Sheet.quarterly[*].commonStockSharesOutstanding`` and
     its annual sibling — last-resort fallback when ``outstandingShares`` is absent.

Quarterly always wins on a date collision (it's the finer signal).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import Any

import pandas as pd
from sqlalchemy import text
from sqlalchemy.orm import Session

from stockscan.db import session_scope

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SharePoint:
    """One reported share count at a reporting-period end date."""

    period_date: date
    shares: int
    kind: str  # 'quarterly' | 'annual'


# ---------------------------------------------------------------------
# Parsing helpers (tolerant of EODHD's mixed string/number fields)
# ---------------------------------------------------------------------
def _to_date(v: Any) -> date | None:
    if not isinstance(v, str) or not v:
        return None
    try:
        return date.fromisoformat(v[:10])
    except ValueError:
        return None


def _to_shares(v: Any) -> int | None:
    """Parse a share count: tolerate strings, floats, ''/'NA'/0/negatives → None."""
    if v is None or v == "" or v == "NA":
        return None
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    if n != n or n <= 0:  # NaN or non-positive is not a usable share count
        return None
    return round(n)  # round(float) → int


def _points_from_outstanding(section: Any, kind: str) -> dict[date, SharePoint]:
    """Parse one ``outstandingShares.<kind>`` sub-object (date-keyed dict of
    ``{date, dateFormatted, shares, sharesMln}`` records)."""
    out: dict[date, SharePoint] = {}
    if not isinstance(section, dict):
        return out
    for rec in section.values():
        if not isinstance(rec, dict):
            continue
        d = _to_date(rec.get("dateFormatted")) or _to_date(rec.get("date"))
        if d is None:
            continue
        shares = _to_shares(rec.get("shares"))
        if shares is None:
            # `sharesMln` is in millions when `shares` is absent/zero.
            mln = rec.get("sharesMln")
            try:
                shares = round(float(mln) * 1_000_000) if mln not in (None, "", "NA") else None
            except (TypeError, ValueError):
                shares = None
        if shares is None or shares <= 0:
            continue
        out[d] = SharePoint(period_date=d, shares=shares, kind=kind)
    return out


def _points_from_balance_sheet(financials: Any, kind: str) -> dict[date, SharePoint]:
    """Fallback: ``Financials.Balance_Sheet.<kind>`` keyed by period date with a
    ``commonStockSharesOutstanding`` field."""
    out: dict[date, SharePoint] = {}
    section = financials.get(kind) if isinstance(financials, dict) else None
    if not isinstance(section, dict):
        return out
    for key, rec in section.items():
        if not isinstance(rec, dict):
            continue
        d = _to_date(rec.get("date")) or _to_date(key)
        if d is None:
            continue
        shares = _to_shares(rec.get("commonStockSharesOutstanding"))
        if shares is None:
            continue
        out[d] = SharePoint(period_date=d, shares=shares, kind=kind)
    return out


def extract_shares_history(payload: dict[str, Any]) -> list[SharePoint]:
    """Pull every point-in-time share count out of an EODHD fundamentals payload.

    Pure: payload in, sorted ``list[SharePoint]`` out (oldest first). Returns
    ``[]`` when nothing usable is present. Quarterly points take precedence over
    annual ones that fall on the same date.
    """
    if not isinstance(payload, dict):
        return []

    merged: dict[date, SharePoint] = {}

    # Annual first so quarterly overwrites on any date collision.
    outstanding = payload.get("outstandingShares")
    if isinstance(outstanding, dict):
        merged.update(_points_from_outstanding(outstanding.get("annual"), "annual"))
        merged.update(_points_from_outstanding(outstanding.get("quarterly"), "quarterly"))

    if not merged:
        financials = payload.get("Financials")
        if isinstance(financials, dict):
            bs = financials.get("Balance_Sheet")
            merged.update(_points_from_balance_sheet(bs, "annual"))
            merged.update(_points_from_balance_sheet(bs, "quarterly"))

    return [merged[d] for d in sorted(merged)]


# ---------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------
_UPSERT_SQL = text(
    """
    INSERT INTO fundamentals_history
        (symbol, period_date, shares_outstanding, period_kind, source, fetched_at)
    VALUES
        (:symbol, :period_date, :shares, :kind, :source, NOW())
    ON CONFLICT (symbol, period_date) DO UPDATE SET
        shares_outstanding = EXCLUDED.shares_outstanding,
        period_kind        = EXCLUDED.period_kind,
        source             = EXCLUDED.source,
        fetched_at         = NOW();
    """
)


def upsert_shares_history(
    symbol: str,
    points: Iterable[SharePoint],
    *,
    source: str = "eodhd",
    session: Session | None = None,
) -> int:
    """Upsert share-count points for ``symbol``. Idempotent on (symbol, date).
    Returns the number of rows written."""
    rows = [
        {
            "symbol": symbol,
            "period_date": p.period_date,
            "shares": int(p.shares),
            "kind": p.kind,
            "source": source,
        }
        for p in points
        if p.shares and p.shares > 0
    ]
    if not rows:
        return 0

    def _run(s: Session) -> int:
        s.execute(_UPSERT_SQL, rows)
        return len(rows)

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)


def store_shares_history_from_payload(
    symbol: str, payload: dict[str, Any], *, session: Session | None = None
) -> int:
    """Extract + upsert in one step. Returns rows written (0 if nothing usable)."""
    points = extract_shares_history(payload)
    if not points:
        return 0
    return upsert_shares_history(symbol, points, session=session)


def get_shares_history(symbol: str, *, session: Session | None = None) -> pd.Series:
    """Reported share counts for ``symbol`` as a ``Series`` indexed by a tz-naive,
    normalized ``DatetimeIndex`` (period end dates), oldest first. Empty Series
    when the symbol has no history."""
    sql = text(
        "SELECT period_date, shares_outstanding FROM fundamentals_history "
        "WHERE symbol = :s ORDER BY period_date"
    )

    def _run(s: Session) -> pd.Series:
        rows = s.execute(sql, {"s": symbol}).all()
        if not rows:
            return pd.Series(dtype="float64")
        idx = pd.DatetimeIndex([pd.Timestamp(r[0]) for r in rows]).normalize()
        return pd.Series([float(r[1]) for r in rows], index=idx, name=symbol)

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)


def backfill_shares_history_from_snapshots(
    symbols: Iterable[str] | None = None,
    *,
    session: Session | None = None,
) -> dict[str, int]:
    """Replay :func:`extract_shares_history` over already-stored ``raw_payload``
    blobs and populate ``fundamentals_history``. **Makes no API calls.**

    ``symbols=None`` processes every snapshot in the table. Returns
    ``{symbol: rows_written}`` for symbols that yielded at least one point.
    """
    import json

    def _run(s: Session) -> dict[str, int]:
        if symbols is None:
            sql = text("SELECT symbol, raw_payload FROM fundamentals_snapshot")
            rows = s.execute(sql).all()
        else:
            syms = sorted({sym for sym in symbols})
            if not syms:
                return {}
            sql = text(
                "SELECT symbol, raw_payload FROM fundamentals_snapshot "
                "WHERE symbol = ANY(:syms)"
            )
            rows = s.execute(sql, {"syms": syms}).all()

        out: dict[str, int] = {}
        for sym, raw in rows:
            if raw is None:
                continue
            payload = raw if isinstance(raw, dict) else _loads(raw, json)
            if payload is None:
                continue
            n = store_shares_history_from_payload(sym, payload, session=s)
            if n:
                out[sym] = n
        log.info(
            "backfill_shares_history: wrote history for %d/%d snapshot(s)",
            len(out), len(rows),
        )
        return out

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)


def _loads(raw: Any, json_mod: Any) -> dict[str, Any] | None:
    """Parse a raw_payload that may already be a dict (JSONB) or a JSON string."""
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json_mod.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except (TypeError, ValueError):
        return None
