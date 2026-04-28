"""Notification router — fan-out to all configured channels.

Channels self-report whether they're configured (via `.is_configured()`),
and the router skips unconfigured ones. So the same code path works
whether the operator has email + Discord, just one, or neither (in which
case messages go to the log only).
"""

from __future__ import annotations

import logging

from stockscan.notify.base import NotificationChannel
from stockscan.notify.discord import DiscordChannel
from stockscan.notify.email import EmailChannel

log = logging.getLogger(__name__)


def default_channels() -> list[NotificationChannel]:
    """Return all channels that have credentials in .env. Order = preference."""
    out: list[NotificationChannel] = []
    email = EmailChannel()
    if email.is_configured():
        out.append(email)
    discord = DiscordChannel()
    if discord.is_configured():
        out.append(discord)
    return out


def notify(
    subject: str,
    body: str,
    *,
    priority: str = "normal",
    channels: list[NotificationChannel] | None = None,
) -> int:
    """Send to every configured channel. Returns number of successful sends."""
    targets = channels if channels is not None else default_channels()
    if not targets:
        log.info("no channels configured — would have sent: %s", subject)
        return 0
    successes = 0
    for ch in targets:
        try:
            ch.send(subject, body, priority=priority)
            successes += 1
        except Exception as exc:  # noqa: BLE001
            # Failure on one channel doesn't block the others.
            log.error("notify channel %s failed: %s", ch.name, exc)
    return successes
