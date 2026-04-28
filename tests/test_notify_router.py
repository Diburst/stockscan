"""Router fan-out semantics."""

from __future__ import annotations

from unittest.mock import MagicMock

from stockscan.notify import notify
from stockscan.notify.base import NotificationChannel


class FakeChannel(NotificationChannel):
    def __init__(self, name: str, fail: bool = False) -> None:
        self.name = name
        self.fail = fail
        self.calls: list[tuple[str, str, str]] = []

    def send(self, subject, body, *, priority="normal"):
        self.calls.append((subject, body, priority))
        if self.fail:
            raise RuntimeError(f"{self.name} simulated failure")


def test_router_sends_to_all_configured():
    a, b = FakeChannel("a"), FakeChannel("b")
    n = notify("hi", "body", channels=[a, b])
    assert n == 2
    assert a.calls == [("hi", "body", "normal")]
    assert b.calls == [("hi", "body", "normal")]


def test_router_continues_after_one_failure():
    a, b = FakeChannel("a", fail=True), FakeChannel("b")
    n = notify("hi", "body", channels=[a, b])
    assert n == 1  # one succeeded, one failed
    assert b.calls == [("hi", "body", "normal")]


def test_router_no_channels_returns_zero():
    n = notify("hi", "body", channels=[])
    assert n == 0
