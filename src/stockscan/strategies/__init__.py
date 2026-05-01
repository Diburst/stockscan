"""Strategy plugin system.

Drop a new .py file into this package; subclass `Strategy`; restart the app.
The scanner, backtester, and analyzer pick it up automatically via
`STRATEGY_REGISTRY` (DESIGN §4.11).
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from pathlib import Path

from stockscan.strategies._signals import (
    ExitDecision,
    PositionSnapshot,
    RawSignal,
    Side,
)
from stockscan.strategies.base import (
    STRATEGY_REGISTRY,
    Strategy,
    StrategyParams,
)
from stockscan.strategies.versions import current_version_filter

log = logging.getLogger(__name__)

__all__ = [
    "STRATEGY_REGISTRY",
    "ExitDecision",
    "PositionSnapshot",
    "RawSignal",
    "Side",
    "Strategy",
    "StrategyParams",
    "current_version_filter",
    "discover_strategies",
]


def discover_strategies() -> int:
    """Import every strategy module in this package.

    Subclassing `Strategy` registers automatically via `__init_subclass__`,
    so importing the modules is sufficient — no explicit registration call.

    Returns the number of registered strategies after discovery.
    """
    pkg_path = Path(__file__).parent
    skip = {"__init__", "_signals", "base", "versions"}
    for module_info in pkgutil.iter_modules([str(pkg_path)]):
        if module_info.name.startswith("_") or module_info.name in skip:
            continue
        full_name = f"{__name__}.{module_info.name}"
        try:
            importlib.import_module(full_name)
        except Exception:
            log.exception("Failed to import strategy module %s", full_name)
            raise
    return len(STRATEGY_REGISTRY)
