"""SMTP email channel — verifies message construction without sending."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from stockscan.notify import EmailChannel


def test_not_configured_drops_silently():
    ch = EmailChannel(from_addr="", to_addr="", smtp_host="", smtp_user="")
    assert ch.is_configured() is False
    # Must not raise
    ch.send("subject", "body")


def test_configured_sends_via_smtp():
    ch = EmailChannel(
        from_addr="me@example.com",
        to_addr="you@example.com",
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_user="user",
        smtp_pass="pass",
    )
    assert ch.is_configured() is True

    smtp_instance = MagicMock()
    smtp_ctx = MagicMock()
    smtp_ctx.__enter__.return_value = smtp_instance
    smtp_ctx.__exit__.return_value = False

    with patch("stockscan.notify.email.smtplib.SMTP", return_value=smtp_ctx) as smtp_class:
        ch.send("Hello", "Body text")

    smtp_class.assert_called_once_with("smtp.example.com", 587, timeout=15)
    smtp_instance.starttls.assert_called_once()
    smtp_instance.login.assert_called_once_with("user", "pass")
    sent_msg = smtp_instance.send_message.call_args[0][0]
    assert sent_msg["Subject"] == "Hello"
    assert sent_msg["From"] == "me@example.com"
    assert sent_msg["To"] == "you@example.com"


def test_high_priority_sets_headers():
    ch = EmailChannel(
        from_addr="a@b.c", to_addr="x@y.z",
        smtp_host="h", smtp_port=25, smtp_user="u", smtp_pass="p",
    )
    smtp_instance = MagicMock()
    smtp_ctx = MagicMock()
    smtp_ctx.__enter__.return_value = smtp_instance
    smtp_ctx.__exit__.return_value = False
    with patch("stockscan.notify.email.smtplib.SMTP", return_value=smtp_ctx):
        ch.send("Drift", "Reconcile mismatch", priority="high")
    msg = smtp_instance.send_message.call_args[0][0]
    assert msg["X-Priority"] == "1"
    assert msg["Importance"] == "High"


def test_html_body_attached_as_html():
    ch = EmailChannel(
        from_addr="a@b.c", to_addr="x@y.z",
        smtp_host="h", smtp_port=25, smtp_user="u", smtp_pass="p",
    )
    smtp_instance = MagicMock()
    smtp_ctx = MagicMock()
    smtp_ctx.__enter__.return_value = smtp_instance
    smtp_ctx.__exit__.return_value = False
    with patch("stockscan.notify.email.smtplib.SMTP", return_value=smtp_ctx):
        ch.send("Hi", "<h1>Headline</h1><p>body</p>")
    msg = smtp_instance.send_message.call_args[0][0]
    # Multipart with html part present
    parts = msg.get_payload()
    assert any(p.get_content_type() == "text/html" for p in parts)
