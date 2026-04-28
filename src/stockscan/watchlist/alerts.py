"""Watchlist alert firing.

`check_and_fire_alerts()` is the single entry point. It finds all watchlist
items where the target has been crossed, sends a notification through the
existing notify() router, and marks each alerted item so it doesn't fire
again until the user re-enables it.

Designed to be safe to re-run (idempotent): once an item fires, alert_enabled
flips to FALSE and it's filtered out of subsequent scans.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from stockscan.notify import notify
from stockscan.notify.base import NotificationChannel
from stockscan.watchlist.store import WatchlistItem, get_triggered, mark_alerted

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AlertResult:
    fired: list[WatchlistItem]


def check_and_fire_alerts(
    *,
    channels: list[NotificationChannel] | None = None,
) -> AlertResult:
    """Fire alerts for any watchlist items whose targets have been crossed.

    For each triggered item:
      1. Send a notification (subject + body) through the configured channels.
      2. Update the row: set last_alerted_at = now, last_triggered_price = close,
         alert_enabled = FALSE (so it doesn't re-fire).
    """
    triggered = get_triggered()
    fired: list[WatchlistItem] = []

    for item in triggered:
        if item.last_close is None:
            continue
        subject = _format_subject(item)
        body = _format_body(item)
        try:
            notify(subject, body, priority="high", channels=channels)
        except Exception as exc:  # noqa: BLE001
            log.error("watchlist alert send failed for %s: %s", item.symbol, exc)
            continue
        mark_alerted(item.watchlist_id, item.last_close)
        fired.append(item)
        log.info(
            "watchlist alert fired: %s crossed %s %s at %s",
            item.symbol, item.target_direction, item.target_price, item.last_close,
        )

    return AlertResult(fired=fired)


# ---------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------
def _format_subject(item: WatchlistItem) -> str:
    direction_word = "above" if item.target_direction == "above" else "below"
    return f"{item.symbol} crossed {direction_word} ${item.target_price:.2f}"


def _format_body(item: WatchlistItem) -> str:
    direction_word = "above" if item.target_direction == "above" else "below"
    pct = f" ({item.pct_change_today:+.2%} today)" if item.pct_change_today is not None else ""
    bar_date = item.last_bar_date.date() if item.last_bar_date else "n/a"
    note_line = f"\nNote: {item.note}" if item.note else ""
    return (
        f"Watchlist alert\n"
        f"\n"
        f"{item.symbol} closed {direction_word} target.\n"
        f"  Target:     ${item.target_price} ({direction_word})\n"
        f"  Last close: ${item.last_close}{pct}\n"
        f"  As of:      {bar_date}\n"
        f"{note_line}\n"
        f"\n"
        f"Alert auto-disabled after firing. Re-enable from the Watchlist page if needed."
    )
