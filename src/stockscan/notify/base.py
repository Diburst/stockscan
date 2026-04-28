"""Notification channel ABC. Concrete channels arrive in Phase 3."""

from __future__ import annotations

from abc import ABC, abstractmethod


class NotificationChannel(ABC):
    name: str

    @abstractmethod
    def send(self, subject: str, body: str, *, priority: str = "normal") -> None:
        """Deliver a notification. `priority` ∈ {'low','normal','high'}."""


class NoopChannel(NotificationChannel):
    """Drops messages. Default while no channels are configured."""

    name = "noop"

    def send(self, subject: str, body: str, *, priority: str = "normal") -> None:
        return
