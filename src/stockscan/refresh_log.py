"""Generic per-scope refresh cooldown.

A tiny DB-backed gate (table ``refresh_log``, migration 0021) so slow-changing
daily fetches — the economic-events calendar, the earnings calendar/trends —
aren't re-pulled on every refresh click. Mirrors the insider cooldown pattern
but generic over a string ``scope``.

Usage::

    if refresh_due("econ_events", cooldown_hours=20, session=s):
        ... do the fetch ...
        mark_refreshed("econ_events", session=s)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from stockscan.db import session_scope

# `to_regclass` returns NULL (not an error) when the table is absent, so this
# probe is safe to run inside an active transaction without poisoning it. It
# lets the cooldown degrade gracefully when migration 0021 hasn't been applied
# yet: refreshes still work (everything is treated as "due"), they just don't
# get the cooldown benefit — and crucially a missing table never aborts the
# whole refresh and rolls back already-fetched bars.
_EXISTS_SQL = text("SELECT to_regclass('refresh_log')")


def _table_exists(s: Session) -> bool:
    return s.execute(_EXISTS_SQL).scalar() is not None


def refresh_due(
    scope: str, *, cooldown_hours: float, session: Session | None = None
) -> bool:
    """True if ``scope`` has never succeeded or its last success is older than
    ``cooldown_hours``. Also True (fail-open) if the refresh_log table is
    missing, so an unapplied migration doesn't break refreshes."""
    sql = text("SELECT last_success FROM refresh_log WHERE scope = :scope")

    def _run(s: Session) -> bool:
        if not _table_exists(s):
            return True
        row = s.execute(sql, {"scope": scope}).first()
        if row is None or row[0] is None:
            return True
        last: datetime = row[0]
        if last.tzinfo is None:  # defensive: treat naive as UTC
            last = last.replace(tzinfo=UTC)
        return (datetime.now(UTC) - last) >= timedelta(hours=cooldown_hours)

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)


def mark_refreshed(scope: str, *, session: Session | None = None) -> None:
    """Record a successful refresh for ``scope`` (arms the cooldown)."""
    sql = text(
        """
        INSERT INTO refresh_log (scope, last_success)
        VALUES (:scope, NOW())
        ON CONFLICT (scope) DO UPDATE SET last_success = NOW();
        """
    )

    def _run(s: Session) -> None:
        if not _table_exists(s):
            return  # migration not applied yet — silently skip arming
        s.execute(sql, {"scope": scope})

    if session is not None:
        _run(session)
        return
    with session_scope() as s:
        _run(s)
