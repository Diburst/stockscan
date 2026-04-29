"""Persistence layer for the market_regime table."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal

from sqlalchemy import text
from sqlalchemy.orm import Session

from stockscan.db import session_scope

RegimeLabel = Literal["trending_up", "trending_down", "choppy", "transitioning"]


@dataclass(frozen=True, slots=True)
class MarketRegime:
    as_of_date: date
    regime: RegimeLabel
    adx: Decimal
    spy_close: Decimal
    spy_sma200: Decimal


_UPSERT_SQL = text(
    """
    INSERT INTO market_regime (as_of_date, regime, adx, spy_close, spy_sma200)
    VALUES (:d, :r, :adx, :close, :sma200)
    ON CONFLICT (as_of_date) DO UPDATE SET
        regime      = EXCLUDED.regime,
        adx         = EXCLUDED.adx,
        spy_close   = EXCLUDED.spy_close,
        spy_sma200  = EXCLUDED.spy_sma200,
        computed_at = NOW();
    """
)

_GET_SQL = text(
    "SELECT as_of_date, regime, adx, spy_close, spy_sma200 "
    "FROM market_regime WHERE as_of_date = :d"
)

_LATEST_SQL = text(
    "SELECT as_of_date, regime, adx, spy_close, spy_sma200 "
    "FROM market_regime ORDER BY as_of_date DESC LIMIT 1"
)


def _row(r: object) -> MarketRegime:
    return MarketRegime(
        as_of_date=r.as_of_date,  # type: ignore[attr-defined]
        regime=r.regime,  # type: ignore[attr-defined]
        adx=Decimal(str(r.adx)),  # type: ignore[attr-defined]
        spy_close=Decimal(str(r.spy_close)),  # type: ignore[attr-defined]
        spy_sma200=Decimal(str(r.spy_sma200)),  # type: ignore[attr-defined]
    )


def upsert_regime(
    as_of: date,
    regime: RegimeLabel,
    adx: float,
    spy_close: float,
    spy_sma200: float,
    *,
    session: Session | None = None,
) -> MarketRegime:
    params = {
        "d": as_of,
        "r": regime,
        "adx": round(adx, 2),
        "close": round(spy_close, 4),
        "sma200": round(spy_sma200, 4),
    }

    def _run(s: Session) -> MarketRegime:
        s.execute(_UPSERT_SQL, params)
        return MarketRegime(
            as_of_date=as_of,
            regime=regime,
            adx=Decimal(str(round(adx, 2))),
            spy_close=Decimal(str(round(spy_close, 4))),
            spy_sma200=Decimal(str(round(spy_sma200, 4))),
        )

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)


def get_regime(as_of: date, *, session: Session | None = None) -> MarketRegime | None:
    """Return the stored regime for `as_of`, or None if not yet computed."""

    def _run(s: Session) -> MarketRegime | None:
        row = s.execute(_GET_SQL, {"d": as_of}).first()
        return _row(row) if row else None

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)


def latest_regime(*, session: Session | None = None) -> MarketRegime | None:
    """Return the most-recently stored regime (any date), or None."""

    def _run(s: Session) -> MarketRegime | None:
        row = s.execute(_LATEST_SQL).first()
        return _row(row) if row else None

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)
