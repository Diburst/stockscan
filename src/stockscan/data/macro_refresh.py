"""Refresh FRED macro series into ``macro_series``.

Shared by ``stockscan refresh macro`` and the nightly job, so the HY OAS
series behind the regime layer's credit-stress flag is never stale in
production.
"""

from __future__ import annotations

import logging
from datetime import date

from stockscan.data.macro_store import upsert_macro_series
from stockscan.data.providers.fred import FredError, FredProvider

log = logging.getLogger(__name__)

HY_OAS = "BAMLH0A0HYM2"  # ICE BofA US High Yield OAS — regime credit-stress flag
# 1-month and 3-month constant-maturity Treasury yields — the risk-free rate
# for the options analysis Black-Scholes strikes.
DEFAULT_MACRO_SERIES: tuple[str, ...] = (HY_OAS, "DGS1MO", "DGS3MO")


def refresh_macro(
    provider: FredProvider,
    series: tuple[str, ...] | list[str],
    start: date,
    end: date,
) -> dict[str, int | None]:
    """Fetch and upsert each series. ``None`` marks a series whose fetch
    failed; the others are still written."""
    out: dict[str, int | None] = {}
    for code in series:
        try:
            rows = provider.get_macro_series(code, start, end)
        except FredError as exc:
            log.warning("macro refresh: %s failed: %s", code, exc)
            out[code] = None
            continue
        out[code] = upsert_macro_series(rows)
    return out
