"""Technical-indicator plugin registry + auto-discovery."""

from __future__ import annotations

import importlib
import logging
import pkgutil
from pathlib import Path

from stockscan.technical.indicators.base import (
    TECH_REGISTRY,
    TechnicalIndicator,
    TechnicalIndicatorParams,
)

log = logging.getLogger(__name__)

__all__ = [
    "TECH_REGISTRY",
    "TechnicalIndicator",
    "TechnicalIndicatorParams",
    "discover_technical_indicators",
]


def discover_technical_indicators() -> int:
    """Import every indicator module in this package; subclassing auto-registers."""
    pkg_path = Path(__file__).parent
    skip = {"__init__", "base"}
    for module_info in pkgutil.iter_modules([str(pkg_path)]):
        if module_info.name.startswith("_") or module_info.name in skip:
            continue
        full_name = f"{__name__}.{module_info.name}"
        try:
            importlib.import_module(full_name)
        except Exception:
            log.exception("Failed to import technical indicator %s", full_name)
            raise
    return len(TECH_REGISTRY)
