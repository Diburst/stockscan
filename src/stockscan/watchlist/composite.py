"""Per-watchlist composite index series (equal-weight + cap-weight).

Each named list gets two synthetic instruments written into the ``bars``
hypertable — exactly the pattern the sector composites use (``$EWSECTOR:<CODE>``):

  - ``$WLEQ:<list_id>`` — equal-weight, daily-rebalanced index of the list members
  - ``$WLCW:<list_id>`` — market-cap-weighted index of the same members

Both are base-100 *absolute* level series. The web layer rebases them to the
left edge of whatever time window the user picks, so the front end can show
"who's outperforming over this period" without the server re-deriving anything.

**Why persist** (Thomas's call): the composite is read on every watchlist page
load and the membership only changes when the user edits the list, so we build it
once (on membership change or a manual Rebuild) and read it cheaply thereafter,
mirroring the sector-composite design. Synthetic ``$`` symbols never enter the
scan universe (that's driven by ``universe_history``, never "all symbols in bars").

**Cap weight is point-in-time.** Weight_i(t) = raw_close_i(t) x shares_i(t),
where shares come from ``fundamentals_history`` (quarterly, forward-filled onto
the trading calendar, held flat before a symbol's earliest filing). Raw
(un-split-adjusted) close x as-reported shares keeps market cap split-consistent;
the index *returns*, by contrast, are computed from split/dividend-adjusted
closes. Weights are renormalized daily over the members that have data, so a name
without a share history simply doesn't contribute to the cap-weight line.

No look-ahead: every input is sliced to ``≤`` its date and the index math is
causal (see :mod:`stockscan.sectors.composite`).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd
from sqlalchemy import text
from sqlalchemy.orm import Session

from stockscan.data.store import get_bars, upsert_bars
from stockscan.db import session_scope
from stockscan.fundamentals.history import get_shares_history
from stockscan.sectors.composite import DEFAULT_BASE, weighted_composite
from stockscan.sectors.store import COMPOSITE_SOURCE, _levels_to_barrows

log = logging.getLogger(__name__)

# Reserved synthetic-symbol prefixes for the two watchlist composites. The '$'
# keeps them out of the scan universe, same as the sector composites.
EQ_PREFIX = "$WLEQ:"
CW_PREFIX = "$WLCW:"

# Market benchmark overlaid on every chart as a third baseline. SPY bars are
# already kept current by the regime/cycles refresh, so this needs no new data.
BENCHMARK_SYMBOL = "SPY"
BENCHMARK_LABEL = "S&P 500 (SPY)"

# How far back to look for member bars when (re)building. We load whatever bars
# exist in the window; watchlist names typically carry ~3y of backfilled history,
# but a generous bound future-proofs longer histories without huge cost.
_LOOKBACK_DAYS = 365 * 12

# We'd like a sector-style floor of 3 members before a composite "starts", but a
# small list (2 names) must still produce a line — so the effective floor is
# min(this, member_count), never below 1.
_DESIRED_MIN_MEMBERS = 3


def eq_symbol(list_id: int) -> str:
    """Equal-weight composite symbol for a list."""
    return f"{EQ_PREFIX}{list_id}"


def cw_symbol(list_id: int) -> str:
    """Cap-weight composite symbol for a list."""
    return f"{CW_PREFIX}{list_id}"


@dataclass(frozen=True, slots=True)
class RebuildResult:
    list_id: int
    members: int
    eq_rows: int
    cw_rows: int
    as_of: date | None


# ---------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------
def _load_close_frames(
    symbols: list[str], start: date, end: date, *, session: Session
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Wide (adjusted_close, raw_close) frames for ``symbols`` over ``[start, end]``.

    Adjusted closes drive the index *returns*; raw closes drive the cap-weight
    *weights* (market cap is split-invariant only on a raw basis). Both share the
    same tz-naive, normalized trading-day index. Symbols with no bars are skipped.
    """
    adj: dict[str, pd.Series] = {}
    raw: dict[str, pd.Series] = {}
    for sym in symbols:
        df = get_bars(sym, start, end, session=session)  # adjust=True default
        if df is None or df.empty or "close" not in df.columns:
            continue
        idx = df.index.tz_convert(None).normalize()
        a = pd.Series(df["close"].astype(float).to_numpy(), index=idx)
        a = a[~a.index.duplicated(keep="last")]
        adj[sym] = a
        # close_raw exists whenever adjust=True applied; fall back to close.
        raw_col = "close_raw" if "close_raw" in df.columns else "close"
        r = pd.Series(df[raw_col].astype(float).to_numpy(), index=idx)
        r = r[~r.index.duplicated(keep="last")]
        raw[sym] = r
    if not adj:
        return pd.DataFrame(), pd.DataFrame()
    adj_df = pd.DataFrame(adj).sort_index()
    raw_df = pd.DataFrame(raw).reindex(index=adj_df.index, columns=adj_df.columns)
    return adj_df, raw_df


def _cap_weight_frame(
    symbols: list[str], raw_closes: pd.DataFrame, *, session: Session
) -> pd.DataFrame | None:
    """Per-symbol, per-day market cap = raw_close x point-in-time shares.

    Shares come from ``fundamentals_history``: the reported step series is
    forward-filled onto the trading calendar (a quarter's count applies until the
    next filing) and back-filled before the earliest filing (held flat — the best
    available estimate for the pre-history tail). A symbol with no share history
    contributes an all-NaN column and is dropped from the cap-weight average.

    Returns ``None`` when *no* member has any share history (the caller then skips
    the cap-weight line entirely rather than emitting a degenerate series).
    """
    if raw_closes.empty:
        return None
    index = raw_closes.index
    caps: dict[str, pd.Series] = {}
    any_history = False
    for sym in symbols:
        if sym not in raw_closes.columns:
            continue
        hist = get_shares_history(sym, session=session)
        if hist.empty:
            caps[sym] = pd.Series(float("nan"), index=index)
            continue
        any_history = True
        # Reindex the step series onto the union of its own dates + the trading
        # calendar, forward-fill (carry each report forward), then restrict to the
        # trading days. bfill handles the pre-first-filing tail (held flat).
        shares_daily = (
            hist[~hist.index.duplicated(keep="last")]
            .reindex(hist.index.union(index))
            .sort_index()
            .ffill()
            .bfill()
            .reindex(index)
        )
        caps[sym] = raw_closes[sym] * shares_daily
    if not any_history:
        return None
    return pd.DataFrame(caps).reindex(index=index, columns=raw_closes.columns)


# ---------------------------------------------------------------------
# Build + persist
# ---------------------------------------------------------------------
def _delete_existing(symbol: str, *, session: Session) -> None:
    """Drop a composite's stored synthetic bars so a rebuild can't leave stale
    tail rows when membership shrinks or a member's history shortens."""
    session.execute(
        text("DELETE FROM bars WHERE symbol = :s AND interval = '1d'"),
        {"s": symbol},
    )


def refresh_watchlist_composites(
    list_id: int,
    *,
    start: date | None = None,
    end: date | None = None,
    base: float = DEFAULT_BASE,
    session: Session | None = None,
) -> RebuildResult:
    """(Re)build both composites for one list and persist them as synthetic bars.

    Reads current membership from ``watchlist_membership`` (so a removed symbol
    drops out and a freshly added one is included), loads member bars + share
    history, builds the equal-weight and cap-weight level series, and upserts each
    as ``$WLEQ:<id>`` / ``$WLCW:<id>``. Idempotent: rebuilding reproduces the same
    series for the same inputs. Always uses the newest stored data (Thomas's
    "always newest caps" choice).
    """
    end = end or date.today()
    start = start or (end - timedelta(days=_LOOKBACK_DAYS))

    def _run(s: Session) -> RebuildResult:
        members = _members(list_id, session=s)
        eq_sym, cw_sym = eq_symbol(list_id), cw_symbol(list_id)
        # A list that's now empty: clear any stale composite and report nothing.
        if not members:
            _delete_existing(eq_sym, session=s)
            _delete_existing(cw_sym, session=s)
            log.info("watchlist composite rebuild: list %s is empty — cleared", list_id)
            return RebuildResult(list_id, 0, 0, 0, None)

        adj, raw = _load_close_frames(members, start, end, session=s)
        if adj.empty:
            _delete_existing(eq_sym, session=s)
            _delete_existing(cw_sym, session=s)
            log.warning(
                "watchlist composite rebuild: list %s has %d member(s) but no bars",
                list_id, len(members),
            )
            return RebuildResult(list_id, len(members), 0, 0, None)

        min_members = max(1, min(_DESIRED_MIN_MEMBERS, len(members)))

        eq_level = weighted_composite(adj, None, base=base, min_members=min_members)
        cap = _cap_weight_frame(members, raw, session=s)
        cw_level = (
            weighted_composite(adj, cap, base=base, min_members=min_members)
            if cap is not None
            else pd.Series(dtype="float64")
        )

        _delete_existing(eq_sym, session=s)
        _delete_existing(cw_sym, session=s)
        eq_rows = upsert_bars(
            _levels_to_barrows(eq_sym, eq_level, source=COMPOSITE_SOURCE), session=s
        )
        cw_rows = (
            upsert_bars(
                _levels_to_barrows(cw_sym, cw_level, source=COMPOSITE_SOURCE), session=s
            )
            if not cw_level.empty
            else 0
        )

        as_of = None
        if not eq_level.dropna().empty:
            as_of = eq_level.dropna().index[-1].date()
        log.info(
            "watchlist composite rebuild: list=%s members=%d eq_rows=%d cw_rows=%d "
            "as_of=%s%s",
            list_id, len(members), eq_rows, cw_rows, as_of,
            "" if cap is not None else " (no share history → cap-weight skipped)",
        )
        return RebuildResult(list_id, len(members), eq_rows, cw_rows, as_of)

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)


def rebuild_for_symbol(symbol: str, *, session: Session | None = None) -> list[RebuildResult]:
    """Rebuild every list that contains ``symbol`` (used after an all-lists
    unwatch, where the symbol may belong to several lists)."""

    def _run(s: Session) -> list[RebuildResult]:
        rows = s.execute(
            text(
                """
                SELECT DISTINCT m.list_id
                FROM watchlist_membership m
                JOIN watchlist_items w ON w.watchlist_id = m.watchlist_id
                WHERE UPPER(w.symbol) = UPPER(:sym)
                """
            ),
            {"sym": symbol},
        ).all()
        return [refresh_watchlist_composites(int(r[0]), session=s) for r in rows]

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)


def backfill_all_lists(*, session: Session | None = None) -> list[RebuildResult]:
    """Build composites for every existing list. Uses only stored bars + stored
    share history — **no API calls** — so it's safe to run against the live DB
    without touching the EODHD budget."""

    def _run(s: Session) -> list[RebuildResult]:
        ids = [int(r[0]) for r in s.execute(text("SELECT list_id FROM watchlists ORDER BY list_id"))]
        results = [refresh_watchlist_composites(lid, session=s) for lid in ids]
        log.info("backfill_all_lists: rebuilt %d list(s)", len(results))
        return results

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)


# ---------------------------------------------------------------------
# Reads (for the web layer)
# ---------------------------------------------------------------------
def _members(list_id: int, *, session: Session) -> list[str]:
    rows = session.execute(
        text(
            """
            SELECT w.symbol
            FROM watchlist_items w
            JOIN watchlist_membership m ON m.watchlist_id = w.watchlist_id
            WHERE m.list_id = :lid
            ORDER BY w.symbol
            """
        ),
        {"lid": list_id},
    ).all()
    return [r[0] for r in rows]


def _level_series(symbol: str, *, session: Session) -> list[dict[str, object]]:
    """Stored composite as ``[{"time": "YYYY-MM-DD", "value": float}, ...]``
    (Lightweight-Charts line-series shape). Empty list if never built."""
    df = get_bars(symbol, date(1990, 1, 1), date.today(), session=session, adjust=False)
    if df is None or df.empty or "close" not in df.columns:
        return []
    idx = df.index.tz_convert(None).normalize()
    return [
        {"time": ts.strftime("%Y-%m-%d"), "value": round(float(v), 4)}
        for ts, v in zip(idx, df["close"].astype(float).to_numpy(), strict=False)
    ]


def _benchmark_series(*, session: Session) -> list[dict[str, object]]:
    """Absolute adjusted-close series for the market benchmark (SPY), for the
    third baseline line. Empty list if SPY bars aren't stored."""
    return member_series([BENCHMARK_SYMBOL], session=session).get(BENCHMARK_SYMBOL, [])


def composite_payload(list_id: int, *, session: Session | None = None) -> dict[str, object]:
    """Everything the watchlist chart needs for one list: the two composite
    series, the S&P 500 benchmark, the member list, and the freshness date.
    Absolute base-100 / price levels — the client rebases to the selected
    window."""

    def _run(s: Session) -> dict[str, object]:
        members = _members(list_id, session=s)
        eq = _level_series(eq_symbol(list_id), session=s)
        cw = _level_series(cw_symbol(list_id), session=s)
        bench = _benchmark_series(session=s)
        as_of = eq[-1]["time"] if eq else (cw[-1]["time"] if cw else None)
        return {
            "list_id": list_id,
            "members": members,
            "built": bool(eq or cw),
            "as_of": as_of,
            "benchmark_label": BENCHMARK_LABEL,
            "series": {"equal_weight": eq, "cap_weight": cw, "benchmark": bench},
        }

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)


def member_series(
    symbols: list[str], *, session: Session | None = None
) -> dict[str, list[dict[str, object]]]:
    """Adjusted-close series for individual member symbols, for overlay lines.
    Absolute prices — the client rebases them to the window like the composites."""

    def _run(s: Session) -> dict[str, list[dict[str, object]]]:
        out: dict[str, list[dict[str, object]]] = {}
        for sym in symbols:
            df = get_bars(sym, date.today() - timedelta(days=_LOOKBACK_DAYS), date.today(), session=s)
            if df is None or df.empty or "close" not in df.columns:
                out[sym] = []
                continue
            idx = df.index.tz_convert(None).normalize()
            out[sym] = [
                {"time": ts.strftime("%Y-%m-%d"), "value": round(float(v), 4)}
                for ts, v in zip(idx, df["close"].astype(float).to_numpy(), strict=False)
            ]
        return out

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)
