"""Equal-weight sector composites for cross-sectional relative strength.

Public surface is the **pure math** (``composite``); the DB-backed builder lives
in ``stockscan.sectors.store`` and is imported explicitly by callers (CLI, jobs)
so that importing this package never drags in the database/config layer — which
keeps ``composite`` unit-testable without infrastructure.
"""

from stockscan.sectors.composite import (
    COMPOSITE_PREFIX,
    build_sector_composites,
    composite_symbol,
    sector_code,
)

__all__ = [
    "COMPOSITE_PREFIX",
    "build_sector_composites",
    "composite_symbol",
    "sector_code",
]
