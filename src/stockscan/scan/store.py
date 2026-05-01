"""Read helpers for the scanner's persisted output.

The scanner itself (``runner.py``) writes ``signals`` and ``strategy_runs``
rows. This module is the read-side companion: thin SQL functions for
the dashboard / Signals page that don't belong inside the runner class.

Currently exposes :func:`signals_freshness` — a small bundle of "how
fresh is the signals table?" facts. Add more (e.g., per-strategy
recency, intraday refresh stats) as new UI surfaces need them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import text

from stockscan.db import session_scope

if TYPE_CHECKING:
    from datetime import date, datetime

    from sqlalchemy.orm import Session


@dataclass(frozen=True, slots=True)
class SignalsFreshness:
    """Freshness facts shown in the Signals page header strip.

    All fields can be ``None`` if the underlying table is empty (e.g.,
    a brand-new install before the first scan run).

    Fields:
      latest_signal_date  — MAX(signals.as_of_date). The most recent
                            scan day represented in the table; what the
                            user is likely already viewing.
      latest_run_at       — MAX(strategy_runs.run_at). When ANY scan
                            runner *finished* most recently. Drives the
                            "Last scan: 14m ago" chip.
      latest_bar_date     — MAX(bars.bar_ts)::date. The latest market
                            data we have for any symbol — the upper
                            bound of what a fresh scan could be based
                            on. If this lags by more than a day or two
                            the user knows to fetch.
      signals_today_count — Number of signals (passing OR rejected) with
                            as_of_date = today. Distinguishes "I ran a
                            scan today and got nothing" from "I haven't
                            run a scan yet today".
    """

    latest_signal_date: date | None
    latest_run_at: datetime | None
    latest_bar_date: date | None
    signals_today_count: int


def signals_freshness(*, session: Session | None = None) -> SignalsFreshness:
    """Cheap aggregate query — 4 scalars, all indexed columns."""

    def _run(s: Session) -> SignalsFreshness:
        # Positional row access (``row[0]``) intentionally — SQLAlchemy
        # 2.x has occasional Row attribute-access quirks on aggregate
        # aliases (see store.py / news/store.py for the same pattern).
        max_sig = s.execute(text("SELECT MAX(as_of_date) FROM signals")).first()
        max_run = s.execute(text("SELECT MAX(run_at) FROM strategy_runs")).first()
        max_bar = s.execute(text("SELECT MAX(bar_ts)::date FROM bars")).first()
        today = s.execute(
            text("SELECT COUNT(*) FROM signals WHERE as_of_date = CURRENT_DATE")
        ).first()
        return SignalsFreshness(
            latest_signal_date=max_sig[0] if max_sig else None,
            latest_run_at=max_run[0] if max_run else None,
            latest_bar_date=max_bar[0] if max_bar else None,
            signals_today_count=int(today[0]) if today and today[0] is not None else 0,
        )

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)
