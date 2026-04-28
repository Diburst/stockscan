"""Discord webhook channel — verifies payload shape without HTTP."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from stockscan.notify import DiscordChannel


def test_not_configured_drops_silently():
    ch = DiscordChannel(webhook_url="")
    assert ch.is_configured() is False
    ch.send("subject", "body")  # must not raise


def test_send_posts_correct_payload():
    ch = DiscordChannel(webhook_url="https://discord.com/api/webhooks/123/abc")
    response = MagicMock()
    response.status_code = 204
    with patch("stockscan.notify.discord.httpx.post", return_value=response) as post:
        ch.send("Title", "Body content", priority="normal")
    post.assert_called_once()
    args, kwargs = post.call_args
    assert args[0] == "https://discord.com/api/webhooks/123/abc"
    payload = kwargs["json"]
    embed = payload["embeds"][0]
    assert embed["title"] == "Title"
    assert embed["description"] == "Body content"
    assert "color" in embed


def test_high_priority_uses_red_color():
    ch = DiscordChannel(webhook_url="https://discord.com/api/webhooks/x")
    response = MagicMock()
    response.status_code = 204
    with patch("stockscan.notify.discord.httpx.post", return_value=response) as post:
        ch.send("Alert", "Important", priority="high")
    embed = post.call_args.kwargs["json"]["embeds"][0]
    assert embed["color"] == 0xDC2626


def test_long_body_is_truncated():
    ch = DiscordChannel(webhook_url="https://discord.com/api/webhooks/x")
    response = MagicMock()
    response.status_code = 204
    huge = "x" * 10_000
    with patch("stockscan.notify.discord.httpx.post", return_value=response) as post:
        ch.send("ok", huge)
    desc = post.call_args.kwargs["json"]["embeds"][0]["description"]
    assert len(desc) <= 4000


def test_4xx_response_raises():
    import httpx
    ch = DiscordChannel(webhook_url="https://discord.com/api/webhooks/x")
    response = MagicMock()
    response.status_code = 400
    response.text = "bad request"
    response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "400", request=MagicMock(), response=response
    )
    with patch("stockscan.notify.discord.httpx.post", return_value=response):
        try:
            ch.send("subject", "body")
            assert False, "expected error"
        except httpx.HTTPStatusError:
            pass
