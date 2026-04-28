"""Shared pytest fixtures.

Phase 0+ tests run without a live database (Mock session in tests that need it).
DB-dependent tests are marked `@pytest.mark.integration` and skipped by default;
run them with `make test-int` once a TimescaleDB instance is available.
"""

from __future__ import annotations

import os

import pytest

# Force a known-good test config before any stockscan modules load.
os.environ.setdefault("STOCKSCAN_ENV", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://stockscan:test@127.0.0.1:5432/stockscan_test")
os.environ.setdefault("EODHD_API_KEY", "")  # forces stub provider in tests


# Populate the strategy registry once at collection time so the autouse fixture
# below has the real (production) strategies in its snapshot.
#
# Why this matters: __init_subclass__ only fires the first time a class is
# defined in the process. If the autouse fixture's snapshot is taken before
# `discover_strategies()` runs, the snapshot is empty — and subsequent
# fixture restores will wipe the registry permanently for later tests.
def _populate_registry_once() -> None:
    from stockscan.strategies import discover_strategies
    discover_strategies()


_populate_registry_once()


@pytest.fixture(autouse=True)
def _isolate_strategy_registry():
    """Ensure each test sees a clean strategy registry.

    Tests that define their own Strategy subclasses would otherwise leak
    into other tests. The snapshot includes the production strategies
    (registered at conftest load), so restoring after a test preserves
    them while wiping any test-only registrations.
    """
    from stockscan.strategies.base import STRATEGY_REGISTRY

    snapshot = dict(STRATEGY_REGISTRY._by_name)
    yield
    STRATEGY_REGISTRY._by_name.clear()
    STRATEGY_REGISTRY._by_name.update(snapshot)
