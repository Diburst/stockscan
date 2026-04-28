"""Discord webhook notification channel.

Uses httpx (already a dep) — no `discord-webhook` library required. The
Discord webhook API is a single POST endpoint that accepts JSON.

Configured via .env:
  DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...

Each `send` posts an embed with color-coded priority. Long bodies are
truncated to Discord's 2,000-char message limit and 4,096-char description
limit; full content goes to the dashboard.
"""

from __future__ import annotations

import logging

import httpx

from stockscan.config import settings
from stockscan.notify.base import NotificationChannel

log = logging.getLogger(__name__)


_PRIORITY_COLORS = {
    # Discord uses decimal RGB for embed colors.
    "low": 0x64748B,      # ink-500 (grey)
    "normal": 0x0F172A,   # ink-900 (near black)
    "high": 0xDC2626,     # bad-600 (red)
}


class DiscordChannel(NotificationChannel):
    name = "discord"

    def __init__(self, webhook_url: str | None = None, *, timeout: float = 10.0) -> None:
        self.webhook_url = webhook_url or settings.discord_webhook_url.get_secret_value()
        self.timeout = timeout

    def is_configured(self) -> bool:
        return bool(self.webhook_url)

    def send(self, subject: str, body: str, *, priority: str = "normal") -> None:
        if not self.is_configured():
            log.debug("DiscordChannel not configured; dropping message: %s", subject)
            return

        payload = {
            "embeds": [
                {
                    "title": subject[:256],
                    "description": body[:4000],
                    "color": _PRIORITY_COLORS.get(priority, _PRIORITY_COLORS["normal"]),
                }
            ]
        }
        try:
            r = httpx.post(self.webhook_url, json=payload, timeout=self.timeout)
            if r.status_code >= 400:
                log.error("discord webhook %s: %s", r.status_code, r.text[:200])
                r.raise_for_status()
            log.info("discord sent: %s", subject)
        except Exception as exc:  # noqa: BLE001
            log.error("discord send failed: %s", exc)
            raise
