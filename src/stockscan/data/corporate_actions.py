"""Corporate action persistence (splits, dividends, spinoffs).

Stored in `corporate_actions`. Splits trigger a re-adjustment of historical
bars (full implementation in Phase 1 alongside the backtester); v0.1 only
captures and persists the actions.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from stockscan.db import session_scope


@dataclass(frozen=True, slots=True)
class CorporateAction:
    symbol: str
    action_date: date
    action_type: str  # 'split' | 'cash_div' | 'stock_div' | 'spinoff'
    ratio: Decimal | None  # for splits, e.g. 2.0 for 2-for-1
    amount: Decimal | None  # for dividends
    raw_payload: dict[str, Any] | None = None


_UPSERT_SQL = text(
    """
    INSERT INTO corporate_actions
        (symbol, action_date, action_type, ratio, amount, raw_payload)
    VALUES
        (:symbol, :action_date, :action_type, :ratio, :amount, CAST(:raw_payload AS JSONB))
    ON CONFLICT (symbol, action_date, action_type) DO UPDATE SET
        ratio       = EXCLUDED.ratio,
        amount      = EXCLUDED.amount,
        raw_payload = EXCLUDED.raw_payload;
    """
)


def upsert_actions(actions: Iterable[CorporateAction], *, session: Session | None = None) -> int:
    """Idempotent upsert of corporate actions."""
    import json

    payload = [
        {
            "symbol": a.symbol,
            "action_date": a.action_date,
            "action_type": a.action_type,
            "ratio": a.ratio,
            "amount": a.amount,
            "raw_payload": json.dumps(a.raw_payload) if a.raw_payload else None,
        }
        for a in actions
    ]
    if not payload:
        return 0
    if session is not None:
        session.execute(_UPSERT_SQL, payload)
        return len(payload)
    with session_scope() as s:
        s.execute(_UPSERT_SQL, payload)
    return len(payload)
