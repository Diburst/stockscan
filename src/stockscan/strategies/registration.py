"""Idempotently register a strategy's `strategy_versions` row.

Both the live scanner (`scan.runner`) and the backtester (`backtest.store`) need
a `strategy_versions` row to exist before they can persist anything that FKs to
it — `signals`, `backtest_runs`, `strategy_configs`. Previously only the scanner
created it, so a brand-new strategy that had only ever been *backtested* hit a
ForeignKeyViolation on persist. Centralizing the upsert here means a single-file
strategy drop "just works" from either entry point.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from sqlalchemy import text
from sqlalchemy.orm import Session

from stockscan.db import session_scope

if TYPE_CHECKING:
    from stockscan.strategies.base import Strategy


_UPSERT_VERSION = text(
    """
    INSERT INTO strategy_versions
        (strategy_name, strategy_version, display_name, description, tags,
         params_json_schema, code_fingerprint)
    VALUES
        (:n, :v, :dn, :d, :t, CAST(:schema AS JSONB), :fp)
    ON CONFLICT (strategy_name, strategy_version) DO NOTHING;
    """
)


def ensure_strategy_version(
    strategy_cls: type[Strategy], *, session: Session | None = None
) -> None:
    """Upsert the strategy's version row (idempotent; ON CONFLICT DO NOTHING)."""
    payload = {
        "n": strategy_cls.name,
        "v": strategy_cls.version,
        "dn": strategy_cls.display_name,
        "d": strategy_cls.description,
        "t": list(strategy_cls.tags),
        "schema": json.dumps(strategy_cls.params_json_schema()),
        "fp": strategy_cls.code_fingerprint(),
    }
    if session is not None:
        session.execute(_UPSERT_VERSION, payload)
        return
    with session_scope() as s:
        s.execute(_UPSERT_VERSION, payload)
