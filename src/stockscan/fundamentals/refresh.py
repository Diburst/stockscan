"""Bulk refresh: pulls fundamentals for a list of symbols, parses, upserts."""

from __future__ import annotations

import logging
from collections.abc import Iterable

from stockscan.data.providers.base import DISABLED_REASON, DataProvider
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
        'skipped'      — provider plan does not include fundamentals
                         (no call made; existing snapshot rows are kept)
    """
    out: dict[str, str] = {}
    if not provider.supports("fundamentals"):
        syms = list(symbols)
        log.info("fundamentals refresh skipped for %d symbols: %s", len(syms), DISABLED_REASON)
        return dict.fromkeys(syms, "skipped")
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
