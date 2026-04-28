"""Notification channels (DESIGN §4.10).

v1 channels: email (SMTP / Postmark) + Discord webhook.
"""

from stockscan.notify.base import NotificationChannel, NoopChannel
from stockscan.notify.discord import DiscordChannel
from stockscan.notify.email import EmailChannel
from stockscan.notify.router import default_channels, notify

__all__ = [
    "NotificationChannel",
    "NoopChannel",
    "EmailChannel",
    "DiscordChannel",
    "default_channels",
    "notify",
]
