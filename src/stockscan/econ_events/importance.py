"""Importance bucketing for economic events.

EODHD's ``event_type`` field is a freeform string ("CPI", "Nonfarm
Payrolls", "FOMC Economic Projections", etc.). The dashboard and the
analysis-detail badge need an ``importance`` filter so they can show
just the high-conviction catalysts without burying them under noise.

We classify by substring match into three buckets:

  * **high**   — known equity-market movers (CPI, NFP, FOMC, PCE, PPI).
                 Always shown.
  * **medium** — meaningful but lower-amplitude (ISM, Retail Sales,
                 GDP, JOLTS, Consumer Confidence, Jobless Claims).
                 Shown on dedicated pages, not the dashboard summary.
  * **low**    — everything else. Kept in the DB for completeness but
                 hidden from the main UI surfaces.

The mapping is intentionally curated rather than auto-derived — the
list is small enough to hand-maintain and the false-positive risk of
substring-based ML classification on macro event names is too high.
"""

from __future__ import annotations


# Curated patterns. Matching is case-insensitive substring search; the
# first match wins, falling through to 'low' if nothing hits.
_HIGH_IMPORTANCE_PATTERNS: tuple[str, ...] = (
    "cpi",  # consumer price index, both headline and core
    "core cpi",
    "core pce",
    "pce",  # personal consumption expenditures (Fed's preferred inflation gauge)
    "ppi",  # producer price index
    "nonfarm payroll",
    "non-farm payroll",
    "unemployment rate",
    "fomc",  # FOMC statement / minutes / projections
    "federal funds",
    "fed funds",
    "interest rate decision",
    "interest rate",
)

_MEDIUM_IMPORTANCE_PATTERNS: tuple[str, ...] = (
    "ism",  # ISM Manufacturing / Services PMI
    "pmi",  # other PMI flavours (S&P Global, Markit)
    "retail sales",
    "gdp",  # GDP growth, GDP price index
    "jolts",  # JOLTS job openings
    "initial jobless claims",
    "continuing jobless claims",
    "consumer confidence",
    "consumer sentiment",
    "michigan",  # University of Michigan sentiment
    "durable goods",
    "industrial production",
    "housing starts",
    "existing home sales",
    "new home sales",
    "trade balance",
    "factory orders",
)


def classify_importance(event_type: str | None) -> str:
    """Return ``"high"`` / ``"medium"`` / ``"low"`` for a given event_type.

    Case-insensitive substring match. Unknown / empty types fall through
    to ``"low"``.
    """
    if not event_type:
        return "low"
    lowered = event_type.lower()
    for pattern in _HIGH_IMPORTANCE_PATTERNS:
        if pattern in lowered:
            return "high"
    for pattern in _MEDIUM_IMPORTANCE_PATTERNS:
        if pattern in lowered:
            return "medium"
    return "low"
