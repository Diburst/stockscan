"""Sector-composite persistence.

Reads constituent bars + point-in-time membership + the sector map from
Postgres, builds the equal-weight composites (pure math in
:mod:`stockscan.sectors.composite`), and upserts each one as a synthetic
``$EWSECTOR:<CODE>`` instrument into the ``bars`` hypertable.

Why synthetic bars rather than a dedicated table (spec §8.3): relative-strength
code already fetches a benchmark *by symbol* via
:func:`stockscan.data.store.get_bars` (see ``donchian_trend._relative_strength``),
so a sector composite is just another symbol — ``sector_rs`` becomes
``get_bars(sector_composite_symbol_for(stock))`` with zero new fetch plumbing,
and every ``indicators/ta.py`` function works on it unchanged. The reserved
``$`` prefix plus ``source='derived'`` keeps these rows out of the scan universe,
which is driven by ``universe_history`` — never by "all symbols in bars".

No look-ahead: prices are point-in-time via ``universe_history`` + bars ``≤``
each date, and the index math is causal (see ``composite.py``). The one accepted
caveat (documented, mirrors DESIGN §4.15): the GICS classification comes from
``fundamentals_snapshot.sector``, a *latest* snapshot rather than per-quarter
history. Sector membership is far stickier than price, so the bias is negligible
for RS; prices stay clean.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import date, datetime, timezone
from decimal import Decimal

import pandas as pd
from sqlalchemy import text
from sqlalchemy.orm import Session

from stockscan.data.providers.base import BarRow
from stockscan.data.store import get_bars, upsert_bars
from stockscan.db import session_scope
from stockscan.sectors.composite import (
    COMPOSITE_PREFIX,
    DEFAULT_BASE,
    build_sector_composites,
    composite_symbol,
)
from stockscan.universe import all_known_symbols

log = logging.getLogger(__name__)

COMPOSITE_SOURCE = "derived"
# Production floor: don't let one or two names define a sector's daily move.
DEFAULT_MIN_MEMBERS = 3
# Default start for a full backfill / nightly rebuild. ~1yr+ of warmup before the
# RS window (63d) means the earliest usable RS reads land in 2008.
DEFAULT_BASE_START = date(2007, 1, 1)


# ---------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------
def sector_map(*, session: Session | None = None) -> dict[str, str]:
    """``{symbol: sector_name}`` for every symbol with a recorded sector."""
    sql = text(
        "SELECT symbol, sector FROM fundamentals_snapshot "
        "WHERE sector IS NOT NULL AND sector <> ''"
    )

    def _run(s: Session) -> dict[str, str]:
        return {row[0]: row[1] for row in s.execute(sql)}

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)


def load_adjusted_closes(
    symbols: Iterable[str],
    start: date,
    end: date,
    *,
    session: Session,
) -> pd.DataFrame:
    """Wide adjusted-close frame: index = trading dates (tz-naive, normalized),
    columns = symbols, values = split/dividend-adjusted close (``NaN`` where a
    symbol has no bar that day). Uses :func:`get_bars` so the same adjustment
    logic the rest of the app sees is applied here.
    """
    series: dict[str, pd.Series] = {}
    for sym in symbols:
        df = get_bars(sym, start, end, session=session)  # adjust=True default
        if df is None or df.empty or "close" not in df.columns:
            continue
        s = df["close"].astype(float)
        # Normalize the UTC bar_ts index to a tz-naive calendar date so every
        # symbol aligns by trading day (and comparisons with DATE columns work).
        s.index = s.index.tz_convert(None).normalize()
        s = s[~s.index.duplicated(keep="last")]
        series[sym] = s
    if not series:
        return pd.DataFrame()
    return pd.DataFrame(series).sort_index()


def build_membership_mask(
    index: pd.DatetimeIndex,
    symbols: Iterable[str],
    *,
    session: Session,
) -> pd.DataFrame:
    """Boolean frame (``index`` × ``symbols``): ``True`` where the symbol was an
    S&P 500 member on that date, from ``universe_history``. A symbol with
    multiple membership spans has them OR-ed together.
    """
    rows = session.execute(
        text("SELECT symbol, joined_date, left_date FROM universe_history")
    ).all()
    cols = list(symbols)
    mask = pd.DataFrame(False, index=index, columns=cols)
    colset = set(cols)
    for sym, joined, left in rows:
        if sym not in colset:
            continue
        active = index >= pd.Timestamp(joined)
        if left is not None:
            active = active & (index < pd.Timestamp(left))
        mask[sym] = mask[sym].to_numpy() | active
    return mask


# ---------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------
def _levels_to_barrows(symbol: str, level: pd.Series, *, source: str) -> list[BarRow]:
    """Flatten an index-level series into synthetic OHLCV bars (O=H=L=C=adj=level,
    volume=0). NaN levels (warmup / flat-gap days that never started) are skipped.
    """
    rows: list[BarRow] = []
    for ts, val in level.items():
        if pd.isna(val):
            continue
        d = pd.Timestamp(ts)
        bar_ts = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
        price = Decimal(str(round(float(val), 6)))
        rows.append(
            BarRow(
                symbol=symbol,
                bar_ts=bar_ts,
                interval="1d",
                open=price,
                high=price,
                low=price,
                close=price,
                adj_close=price,
                volume=0,
                source=source,
            )
        )
    return rows


def refresh_sector_composites(
    start: date,
    end: date,
    *,
    session: Session | None = None,
    min_members: int = DEFAULT_MIN_MEMBERS,
    base: float = DEFAULT_BASE,
) -> dict[str, int]:
    """Build every equal-weight sector composite over ``[start, end]`` and upsert
    them as ``$EWSECTOR:<CODE>`` bars. Returns ``{composite_symbol: rows_written}``.

    Idempotent (upsert on the bars PK), so a full rebuild reproduces the same
    series — which is exactly what the truncation property test guarantees, and
    what makes incremental nightly appends leak-free.
    """

    def _run(s: Session) -> dict[str, int]:
        sec_of = sector_map(session=s)
        symbols = sorted(set(all_known_symbols(session=s)) & set(sec_of))
        if not symbols:
            log.warning("refresh_sector_composites: no universe symbols carry a sector")
            return {}
        closes = load_adjusted_closes(symbols, start, end, session=s)
        if closes.empty:
            log.warning("refresh_sector_composites: no bars in [%s, %s]", start, end)
            return {}
        membership = build_membership_mask(closes.index, closes.columns, session=s)
        levels = build_sector_composites(
            closes, sec_of, membership=membership, base=base, min_members=min_members
        )
        counts: dict[str, int] = {}
        for code, level in levels.items():
            sym = f"{COMPOSITE_PREFIX}{code}"
            counts[sym] = upsert_bars(
                _levels_to_barrows(sym, level, source=COMPOSITE_SOURCE), session=s
            )
        log.info("refresh_sector_composites: wrote %d composites", len(counts))
        return counts

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)


# ---------------------------------------------------------------------
# Lookup (used by the sector_rs indicator at scan time)
# ---------------------------------------------------------------------
def sector_composite_symbol_for(symbol: str, *, session: Session | None = None) -> str | None:
    """Resolve a stock → its ``$EWSECTOR:<CODE>`` benchmark symbol, or ``None`` if
    the symbol has no recorded sector (in which case ``sector_rs`` abstains).
    """

    def _run(s: Session) -> str | None:
        row = s.execute(
            text("SELECT sector FROM fundamentals_snapshot WHERE symbol = :s"),
            {"s": symbol},
        ).first()
        if row is None or row[0] is None or not str(row[0]).strip():
            return None
        return composite_symbol(str(row[0]))

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)
