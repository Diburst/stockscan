"""Shared EODHD provider construction for the write/refresh tools.

Mirrors the CLI's provider helper but raises a typed error (instead of printing)
when no API key is configured, so tools can return a structured error.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from stockscan.config import settings


class NoApiKeyError(RuntimeError):
    """Raised when EODHD_API_KEY is not configured."""


@contextmanager
def provider_ctx() -> Iterator[Any]:
    """Yield an EODHDProvider built from settings; close it on exit.

    Raises NoApiKeyError if EODHD_API_KEY is unset — the refresh/backfill tools
    catch this and return ``{"error": "no_api_key"}`` rather than crashing.
    """
    key = settings.eodhd_api_key.get_secret_value()
    if not key:
        raise NoApiKeyError("EODHD_API_KEY is not set")
    from stockscan.data.providers import EODHDProvider

    provider = EODHDProvider(api_key=key)
    try:
        yield provider
    finally:
        close = getattr(provider, "close", None)
        if callable(close):
            close()
