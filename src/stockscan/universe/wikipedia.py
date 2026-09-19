"""S&P 500 membership from Wikipedia — the fallback universe source.

EODHD serves index components through ``/fundamentals/GSPC.INDX``, which is
a Fundamentals-plan endpoint. On an EOD-only plan (``EODHD_FEATURES`` without
``universe``) we can't call it, but the scanner still needs to know when the
index turns over: a name added to the S&P 500 that never reaches
``universe_history`` is filtered out of every bulk bar refresh and no
strategy will ever see it.

Wikipedia's "List of S&P 500 companies" page carries two tables we can use:

* **constituents** — the current roster with a "Date added" column.
* **changes** — the log of additions/removals with an effective date.

Merge semantics are deliberately *incremental*, not a full re-upsert:

1. Every symbol on the current roster that has no open interval in
   ``universe_history`` (no row with ``left_date IS NULL``) gets one,
   dated from the roster's "Date added" (or, when that is blank, the most
   recent matching row in the changes table, or today).
2. Every open interval in ``universe_history`` whose symbol is *not* on the
   current roster is closed, dated from the changes table when it lists the
   removal, otherwise today.

Everything else — the EODHD-sourced history back to ~2000 — is left exactly
as it is. Wikipedia only keeps the frontier honest; it never rewrites the
past. That also means switching back to the EODHD source after an upgrade
is safe: its full upsert simply overlays the same intervals.

Ticker convention: Wikipedia writes share classes with a dot (``BRK.B``),
EODHD (and therefore our ``bars`` table) with a dash (``BRK-B``). We
normalise to the EODHD form so the symbols line up with the bar store.

Parsing uses only the standard library (``html.parser``) — no bs4/lxml
dependency for a page we read once a week.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from html.parser import HTMLParser
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session

from stockscan.db import session_scope

log = logging.getLogger(__name__)

WIKIPEDIA_SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
_USER_AGENT = "stockscan/0.1 (personal research tool; universe refresh)"


# ----------------------------------------------------------------------
# Data shapes
# ----------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class RosterEntry:
    symbol: str  # EODHD form (BRK-B)
    date_added: date | None


@dataclass(frozen=True, slots=True)
class ChangeEntry:
    effective: date
    added: str | None  # EODHD form
    removed: str | None


@dataclass(frozen=True, slots=True)
class WikiUniverse:
    roster: list[RosterEntry]
    changes: list[ChangeEntry]


@dataclass(frozen=True, slots=True)
class WikiRefreshSummary:
    """What the incremental merge actually did."""

    roster_size: int
    opened: tuple[str, ...]  # symbols given a new open interval
    closed: tuple[str, ...]  # symbols whose open interval was closed

    @property
    def rows_touched(self) -> int:
        return len(self.opened) + len(self.closed)


# ----------------------------------------------------------------------
# HTML → tables (stdlib only)
# ----------------------------------------------------------------------
class _TableParser(HTMLParser):
    """Collect every ``<table>`` on the page as a list of rows of cell
    text, expanding ``rowspan``/``colspan`` so each logical cell lands in
    its real column (the changes table spans its date cell across several
    rows when multiple changes share an effective date)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._rows: list[list[str]] | None = None
        self._row: list[str | None] | None = None
        self._cell: list[str] | None = None
        self._cell_span: tuple[int, int] = (1, 1)
        # (col → (remaining_rows, text)) for cells spanning down from above.
        self._pending: dict[int, tuple[int, str]] = {}
        self._depth = 0  # nested-table guard: we only read top-level tables

    # -- element boundaries --------------------------------------------
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            self._depth += 1
            if self._depth == 1:
                self._rows = []
                self._pending = {}
            return
        if self._depth != 1 or self._rows is None:
            return
        if tag == "tr":
            self._row = []
            self._fill_pending()
        elif tag in ("td", "th") and self._row is not None:
            a = dict(attrs)
            rs = _int_attr(a.get("rowspan"))
            cs = _int_attr(a.get("colspan"))
            self._cell_span = (rs, cs)
            self._cell = []
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag == "table":
            if self._depth == 1 and self._rows is not None:
                self.tables.append([[c or "" for c in r] for r in self._rows])
                self._rows = None
            self._depth = max(0, self._depth - 1)
            return
        if self._depth != 1:
            return
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            txt = _clean(" ".join(self._cell))
            rs, cs = self._cell_span
            for _ in range(cs):
                col = len(self._row)
                self._row.append(txt)
                if rs > 1:
                    self._pending[col] = (rs - 1, txt)
                self._fill_pending()
            self._cell = None
        elif tag == "tr" and self._row is not None and self._rows is not None:
            self._fill_pending()
            self._rows.append(list(self._row))
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    # -- rowspan bookkeeping ---------------------------------------------
    def _fill_pending(self) -> None:
        """Drop any cell spanning down from a previous row into the
        current column position(s), as far as they run contiguously."""
        if self._row is None:
            return
        while True:
            col = len(self._row)
            hit = self._pending.get(col)
            if hit is None:
                return
            remaining, txt = hit
            self._row.append(txt)
            if remaining - 1 > 0:
                self._pending[col] = (remaining - 1, txt)
            else:
                del self._pending[col]


def _int_attr(v: str | None) -> int:
    try:
        return max(1, int((v or "1").strip()))
    except ValueError:
        return 1


_FOOTNOTE = re.compile(r"\[\s*[^\]]{1,6}\s*\]")  # [1], [a], [note 2]
_WS = re.compile(r"\s+")


def _clean(s: str) -> str:
    return _WS.sub(" ", _FOOTNOTE.sub("", s)).strip()


def _normalize_symbol(raw: str) -> str | None:
    """Wikipedia ``BRK.B`` → EODHD ``BRK-B``; drop anything that isn't a ticker."""
    s = _clean(raw).upper().replace(".", "-")
    return s if re.fullmatch(r"[A-Z][A-Z0-9\-]{0,9}", s) else None


def _parse_iso(raw: str) -> date | None:
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", raw)
    if not m:
        return None
    try:
        return date(int(m[1]), int(m[2]), int(m[3]))
    except ValueError:
        return None


def _parse_long_date(raw: str) -> date | None:
    """``June 30, 2026`` (the changes table) — tolerate stray text."""
    m = re.search(r"([A-Z][a-z]+)\s+(\d{1,2}),\s+(\d{4})", raw)
    if not m:
        return _parse_iso(raw)
    try:
        return datetime.strptime(f"{m[1]} {m[2]} {m[3]}", "%B %d %Y").date()
    except ValueError:
        return None


# ----------------------------------------------------------------------
# Tables → WikiUniverse
# ----------------------------------------------------------------------
def parse_sp500_page(html: str) -> WikiUniverse:
    """Extract the current roster + the changes log from the page HTML.

    Tables are located by their header text, not by position or ``id``,
    so a reshuffle of the page doesn't silently break us.
    """
    parser = _TableParser()
    parser.feed(html)

    roster: list[RosterEntry] = []
    changes: list[ChangeEntry] = []

    for table in parser.tables:
        if len(table) < 2:
            continue
        head = [h.lower() for h in table[0]]
        if head[:2] == ["symbol", "security"]:
            roster = _parse_roster(table)
        elif head and head[0] in ("effective date", "date"):
            changes = _parse_changes(table)

    if not roster:
        raise ValueError("Wikipedia S&P 500 page: constituents table not found")
    return WikiUniverse(roster=roster, changes=changes)


def _parse_roster(table: list[list[str]]) -> list[RosterEntry]:
    head = [h.lower() for h in table[0]]
    sym_i = head.index("symbol")
    try:
        added_i = head.index("date added")
    except ValueError:
        added_i = -1
    out: list[RosterEntry] = []
    seen: set[str] = set()
    for row in table[1:]:
        if len(row) <= sym_i:
            continue
        sym = _normalize_symbol(row[sym_i])
        if not sym or sym in seen:
            continue
        seen.add(sym)
        added = _parse_iso(row[added_i]) if 0 <= added_i < len(row) else None
        out.append(RosterEntry(symbol=sym, date_added=added))
    return out


def _parse_changes(table: list[list[str]]) -> list[ChangeEntry]:
    # Two header rows: [Effective Date, Added, Added, Removed, Removed, Reason]
    # then [Ticker, Security, Ticker, Security] (rowspan'd date/reason).
    # After span expansion the data rows are:
    #   [date, added_ticker, added_security, removed_ticker, removed_security, reason]
    out: list[ChangeEntry] = []
    for row in table[1:]:
        if len(row) < 4:
            continue
        eff = _parse_long_date(row[0])
        if eff is None:
            continue  # the second header row, or a malformed line
        added = _normalize_symbol(row[1]) if row[1] else None
        removed = _normalize_symbol(row[3]) if row[3] else None
        if added is None and removed is None:
            continue
        out.append(ChangeEntry(effective=eff, added=added, removed=removed))
    return out


# ----------------------------------------------------------------------
# Fetch
# ----------------------------------------------------------------------
def fetch_sp500_page(*, client: httpx.Client | None = None, timeout: float = 30.0) -> str:
    """GET the page HTML. ``client`` is the test injection point."""
    own = client is None
    c = client or httpx.Client(timeout=timeout, headers={"User-Agent": _USER_AGENT})
    try:
        resp = c.get(WIKIPEDIA_SP500_URL)
        resp.raise_for_status()
        return resp.text
    finally:
        if own:
            c.close()


# ----------------------------------------------------------------------
# Merge into universe_history
# ----------------------------------------------------------------------
_OPEN_SQL = text("SELECT symbol, joined_date FROM universe_history WHERE left_date IS NULL")
_INSERT_SQL = text(
    """
    INSERT INTO universe_history (symbol, joined_date, left_date)
    VALUES (:symbol, :joined_date, NULL)
    ON CONFLICT (symbol, joined_date) DO UPDATE SET left_date = NULL
    """
)
_CLOSE_SQL = text(
    """
    UPDATE universe_history SET left_date = :left_date
    WHERE symbol = :symbol AND left_date IS NULL
    """
)


def merge_universe(
    uni: WikiUniverse,
    *,
    session: Session | None = None,
    today: date | None = None,
) -> WikiRefreshSummary:
    """Apply the incremental merge described in the module docstring."""
    today = today or date.today()
    roster = {r.symbol: r for r in uni.roster}

    # Latest add / remove date per symbol from the changes log.
    last_added: dict[str, date] = {}
    last_removed: dict[str, date] = {}
    for ch in uni.changes:
        if ch.added and (ch.added not in last_added or ch.effective > last_added[ch.added]):
            last_added[ch.added] = ch.effective
        if ch.removed and (
            ch.removed not in last_removed or ch.effective > last_removed[ch.removed]
        ):
            last_removed[ch.removed] = ch.effective

    def _run(s: Session) -> WikiRefreshSummary:
        open_now = {row[0]: row[1] for row in s.execute(_OPEN_SQL)}

        to_open: list[dict[str, Any]] = []
        for sym, entry in roster.items():
            if sym in open_now:
                continue
            joined = entry.date_added or last_added.get(sym) or today
            to_open.append({"symbol": sym, "joined_date": joined})

        to_close: list[dict[str, Any]] = []
        for sym in open_now:
            if sym in roster:
                continue
            left = last_removed.get(sym) or today
            to_close.append({"symbol": sym, "left_date": left})

        if to_open:
            s.execute(_INSERT_SQL, to_open)
        if to_close:
            s.execute(_CLOSE_SQL, to_close)
        return WikiRefreshSummary(
            roster_size=len(roster),
            opened=tuple(sorted(r["symbol"] for r in to_open)),
            closed=tuple(sorted(r["symbol"] for r in to_close)),
        )

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)


def refresh_universe_from_wikipedia(
    *,
    html: str | None = None,
    session: Session | None = None,
    today: date | None = None,
) -> WikiRefreshSummary:
    """Fetch (unless ``html`` is given), parse, merge. One HTTP request."""
    page = html if html is not None else fetch_sp500_page()
    uni = parse_sp500_page(page)
    if len(uni.roster) < 400:
        # A page-layout change that drops half the roster must not close
        # 100+ live intervals. Refuse loudly instead.
        raise ValueError(
            f"Wikipedia roster parsed to only {len(uni.roster)} symbols — "
            "refusing to merge (page layout changed?)"
        )
    summary = merge_universe(uni, session=session, today=today)
    log.info(
        "universe (wikipedia): roster %d, opened %d (%s), closed %d (%s)",
        summary.roster_size,
        len(summary.opened),
        ", ".join(summary.opened) or "-",
        len(summary.closed),
        ", ".join(summary.closed) or "-",
    )
    return summary
