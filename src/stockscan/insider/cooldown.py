"""23-hour cooldown gate for insider-transaction refreshes.

Backed by ``insider_refresh_log`` (migration 0019). Each refresh
attempt writes a row at ``start_refresh`` and the completion is recorded
at ``finish_refresh``. ``can_refresh`` checks the most recent
*successful* row for the given scope — so a failed run doesn't block
the next attempt the way an in-process timer would, but a successful
run does (correctly).

The scope strings are conventionally:

  * ``"watchlist"`` — the watchlist-wide refresh fired from the
    /watchlist/refresh-bars endpoint.
  * ``"symbol:XYZ"`` — a single-symbol refresh fired from the analysis
    page's on-demand button.

This survives app restarts AND page reloads because the timestamp
lives in the DB, not in process memory or a request session.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import text

from stockscan.db import session_scope

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

log = logging.getLogger(__name__)


# The cooldown was specified as "once daily" by the user. 23 hours gives
# a small safety margin so a nightly cron at 09:00 doesn't drift to 09:01
# and block the next morning's refresh as "too soon".
REFRESH_COOLDOWN_HOURS = 23


_LAST_SUCCESS_SQL = text(
    """
    SELECT completed_at
    FROM insider_refresh_log
    WHERE scope = :scope AND success = TRUE
    ORDER BY completed_at DESC
    LIMIT 1
    """
)


def last_successful_refresh(
    scope: str,
    *,
    session: Session | None = None,
) -> datetime | None:
    """The most recent successful ``completed_at`` for ``scope``, or None."""

    def _run(s: Session) -> datetime | None:
        row = s.execute(_LAST_SUCCESS_SQL, {"scope": scope}).first()
        if row is None or row[0] is None:
            return None
        return row[0]

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)


def can_refresh(
    scope: str,
    *,
    cooldown_hours: float = REFRESH_COOLDOWN_HOURS,
    session: Session | None = None,
) -> tuple[bool, float | None]:
    """Check whether a refresh for ``scope`` is allowed right now.

    Returns ``(allowed, cooldown_remaining_seconds)``:

      * ``(True, None)`` — no successful refresh recorded in the cooldown
        window; the caller may proceed.
      * ``(False, secs)`` — the caller MUST NOT call the upstream API.
        ``secs`` is the wait time until the cooldown lifts.
    """
    last = last_successful_refresh(scope, session=session)
    if last is None:
        return True, None
    # Naïvely subtract; ``completed_at`` is a tz-aware TIMESTAMPTZ.
    now = datetime.now(UTC)
    if last.tzinfo is None:  # defensive — older rows may be naïve
        last = last.replace(tzinfo=UTC)
    elapsed = now - last
    cooldown = timedelta(hours=cooldown_hours)
    if elapsed >= cooldown:
        return True, None
    remaining = (cooldown - elapsed).total_seconds()
    return False, remaining


_START_SQL = text(
    """
    INSERT INTO insider_refresh_log (scope, started_at)
    VALUES (:scope, NOW())
    RETURNING refresh_id
    """
)


def start_refresh(
    scope: str,
    *,
    session: Session | None = None,
) -> int:
    """Record the start of a refresh attempt. Returns the new refresh_id."""

    def _run(s: Session) -> int:
        return int(s.execute(_START_SQL, {"scope": scope}).scalar_one())

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)


_FINISH_SQL = text(
    """
    UPDATE insider_refresh_log
    SET completed_at = NOW(),
        success = :success,
        symbols_refreshed = :symbols_refreshed,
        transactions_upserted = :transactions_upserted,
        error_message = :error_message
    WHERE refresh_id = :refresh_id
    """
)


def finish_refresh(
    refresh_id: int,
    *,
    success: bool,
    symbols_refreshed: int = 0,
    transactions_upserted: int = 0,
    error_message: str | None = None,
    session: Session | None = None,
) -> None:
    """Mark a refresh attempt complete. ``success=True`` arms the cooldown."""

    def _run(s: Session) -> None:
        s.execute(
            _FINISH_SQL,
            {
                "refresh_id": refresh_id,
                "success": success,
                "symbols_refreshed": symbols_refreshed,
                "transactions_upserted": transactions_upserted,
                "error_message": error_message,
            },
        )

    if session is not None:
        _run(session)
        return
    with session_scope() as s:
        _run(s)
