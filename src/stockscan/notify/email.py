"""SMTP email notification channel.

Uses stdlib `smtplib` (no extra deps). Configured via .env:
  NOTIFY_EMAIL_FROM, NOTIFY_EMAIL_TO, plus one of:
    - SMTP server settings (host/port/user/pass), or
    - Postmark token (uses Postmark's SMTP relay smtp.postmarkapp.com)

For Postmark, set:
  SMTP_HOST=smtp.postmarkapp.com
  SMTP_PORT=587
  SMTP_USER=<your-postmark-server-token>
  SMTP_PASS=<same-postmark-server-token>
"""

from __future__ import annotations

import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from stockscan.config import settings
from stockscan.notify.base import NotificationChannel

log = logging.getLogger(__name__)


class EmailChannel(NotificationChannel):
    name = "email"

    def __init__(
        self,
        from_addr: str | None = None,
        to_addr: str | None = None,
        smtp_host: str | None = None,
        smtp_port: int | None = None,
        smtp_user: str | None = None,
        smtp_pass: str | None = None,
        use_tls: bool = True,
    ) -> None:
        self.from_addr = from_addr or settings.notify_email_from
        self.to_addr = to_addr or settings.notify_email_to
        self.host = smtp_host or os.environ.get("SMTP_HOST", "")
        self.port = smtp_port or int(os.environ.get("SMTP_PORT", "587"))
        self.user = smtp_user or os.environ.get("SMTP_USER", "")
        self.password = smtp_pass or os.environ.get("SMTP_PASS", "")
        self.use_tls = use_tls

    def is_configured(self) -> bool:
        return bool(self.from_addr and self.to_addr and self.host and self.user)

    def send(self, subject: str, body: str, *, priority: str = "normal") -> None:
        if not self.is_configured():
            log.debug("EmailChannel not configured; dropping message: %s", subject)
            return

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = self.from_addr
        msg["To"] = self.to_addr
        if priority == "high":
            msg["X-Priority"] = "1"
            msg["Importance"] = "High"

        # Treat body as HTML if it looks like HTML, else plain.
        if body.lstrip().startswith("<"):
            msg.attach(MIMEText(body, "html"))
        else:
            msg.attach(MIMEText(body, "plain"))

        try:
            with smtplib.SMTP(self.host, self.port, timeout=15) as smtp:
                if self.use_tls:
                    smtp.starttls()
                if self.user:
                    smtp.login(self.user, self.password)
                smtp.send_message(msg)
            log.info("email sent: %s → %s", subject, self.to_addr)
        except Exception as exc:  # noqa: BLE001
            log.error("email send failed: %s", exc)
            raise
