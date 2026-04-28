"""Persistence layer for technical_scores. Idempotent upsert on the
(symbol, as_of_date, strategy_name) primary key — re-running a scan
overwrites with the latest computation."""

from __future__ import annotations

import json
from datetime import date

from sqlalchemy import text
from sqlalchemy.orm import Session

from stockscan.db import session_scope
from stockscan.technical.score import TechnicalScore

NEUTRAL_STRATEGY_KEY = "_neutral"  # used by the watchlist (no strategy)


_UPSERT_SQL = text(
    """
    INSERT INTO technical_scores
        (symbol, as_of_date, strategy_name, score, breakdown, computed_at)
    VALUES
        (:symbol, :as_of, :strategy, :score, CAST(:breakdown AS JSONB), NOW())
    ON CONFLICT (symbol, as_of_date, strategy_name) DO UPDATE SET
        score = EXCLUDED.score,
        breakdown = EXCLUDED.breakdown,
        computed_at = NOW();
    """
)


def upsert_score(
    symbol: str,
    as_of: date,
    strategy_name: str,
    score: TechnicalScore,
    *,
    session: Session | None = None,
) -> None:
    payload = {
        "symbol": symbol,
        "as_of": as_of,
        "strategy": strategy_name,
        "score": score.score,
        "breakdown": json.dumps(score.to_breakdown_json()),
    }
    if session is not None:
        session.execute(_UPSERT_SQL, payload)
        return
    with session_scope() as s:
        s.execute(_UPSERT_SQL, payload)
