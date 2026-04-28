"""Bulk refresh: pulls fundamentals for a list of symbols, parses, upserts."""

from __future__ import annotations

import logging
from collections.abc import Iterable

from stockscan.data.providers.base import DataProvider
from stockscan.fundamentals.store import upsert_fundamentals

log = logging.getLogger(__name__)


def refresh_fundamentals(
    provider: DataProvider,
    symbols: Iterable[str],
) -> dict[str, str]:
    """Fetch fundamentals for each symbol; upsert; return per-symbol status.

    Status values:
        'ok'           — fetched and persisted
        'missing'      — provider returned no data for this symbol
        'error'        — fetch or persist threw an exception (logged)
    """
    out: dict[str, str] = {}
    for sym in symbols:
        try:
            payload = provider.get_fundamentals(sym)
        except Exception as exc:  # noqa: BLE001
            log.error("fundamentals fetch failed for %s: %s", sym, exc)
            out[sym] = "error"
            continue
        if not payload:
            out[sym] = "missing"
            continue
        try:
            upsert_fundamentals(sym, payload)
            out[sym] = "ok"
        except Exception as exc:  # noqa: BLE001
            log.error("fundamentals upsert failed for %s: %s", sym, exc)
            out[sym] = "error"
    return out
