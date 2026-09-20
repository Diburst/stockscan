"""Persistence for the ``market_regime`` table (one row per trading day).

A row is the day's two market-health controls plus the inputs behind them,
so the dashboard, the nightly summary and the sizing code all read one
cached record instead of recomputing:

* ``trend_gate_open`` / ``days_on_side`` — the SPY 200-day gate with dwell.
* ``vol_scalar`` with ``realized_vol_20d`` / ``realized_vol_pct_rank``.
* ``credit_stress_flag`` with ``hy_oas_level`` / ``hy_oas_pct_rank``.
* ``regime`` — the display label derived from the flags
  (``risk_on`` / ``risk_off`` / ``credit_stress``).

``methodology_version`` lets :func:`stockscan.regime.detect.detect_regime`
recompute rows written by an older rule set.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import text

from stockscan.db import session_scope

if TYPE_CHECKING:
    from datetime import date

    from sqlalchemy.orm import Session

RegimeLabel = Literal["risk_on", "risk_off", "credit_stress"]

METHODOLOGY_VERSION = 3


def regime_label(*, trend_gate_open: bool, credit_stress_flag: bool) -> RegimeLabel:
    """Display label. Credit stress dominates because it blocks longs outright."""
    if credit_stress_flag:
        return "credit_stress"
    return "risk_on" if trend_gate_open else "risk_off"


@dataclass(frozen=True, slots=True)
class MarketRegime:
    as_of_date: date
    regime: RegimeLabel
    trend_gate_open: bool
    days_on_side: int
    spy_close: Decimal
    spy_sma200: Decimal
    spy_sma200_slope_20d: Decimal | None
    realized_vol_20d: Decimal | None
    realized_vol_pct_rank: Decimal | None
    vol_scalar: Decimal | None
    hy_oas_level: Decimal | None
    hy_oas_pct_rank: Decimal | None
    credit_stress_flag: bool
    methodology_version: int = METHODOLOGY_VERSION

    @property
    def block_new_longs(self) -> bool:
        """New long entries are refused while the gate is closed or credit
        stress fires. Open positions are unaffected."""
        return self.credit_stress_flag or not self.trend_gate_open

    @property
    def vol_multiplier(self) -> float:
        """The vol scalar as a float, neutral when it could not be computed."""
        return float(self.vol_scalar) if self.vol_scalar is not None else 1.0


_COLUMNS = (
    "as_of_date, regime, trend_gate_open, days_on_side, spy_close, spy_sma200, "
    "spy_sma200_slope_20d, realized_vol_20d, realized_vol_pct_rank, vol_scalar, "
    "hy_oas_level, hy_oas_pct_rank, credit_stress_flag, methodology_version"
)

_UPSERT_SQL = text(
    f"""
    INSERT INTO market_regime ({_COLUMNS})
    VALUES (
        :d, :regime, :gate_open, :days_on_side, :close, :sma200,
        :sma_slope, :rv, :rv_rank, :vol_scalar,
        :hy_oas, :hy_rank, :stress, :methver
    )
    ON CONFLICT (as_of_date) DO UPDATE SET
        regime                = EXCLUDED.regime,
        trend_gate_open       = EXCLUDED.trend_gate_open,
        days_on_side          = EXCLUDED.days_on_side,
        spy_close             = EXCLUDED.spy_close,
        spy_sma200            = EXCLUDED.spy_sma200,
        spy_sma200_slope_20d  = EXCLUDED.spy_sma200_slope_20d,
        realized_vol_20d      = EXCLUDED.realized_vol_20d,
        realized_vol_pct_rank = EXCLUDED.realized_vol_pct_rank,
        vol_scalar            = EXCLUDED.vol_scalar,
        hy_oas_level          = EXCLUDED.hy_oas_level,
        hy_oas_pct_rank       = EXCLUDED.hy_oas_pct_rank,
        credit_stress_flag    = EXCLUDED.credit_stress_flag,
        methodology_version   = EXCLUDED.methodology_version,
        computed_at           = NOW();
    """
)

_GET_SQL = text(f"SELECT {_COLUMNS} FROM market_regime WHERE as_of_date = :d")
_LATEST_SQL = text(f"SELECT {_COLUMNS} FROM market_regime ORDER BY as_of_date DESC LIMIT 1")


def _opt_decimal(v: object) -> Decimal | None:
    return Decimal(str(v)) if v is not None else None


def _opt_round(value: float | None, places: int) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(round(value, places)))


def _row(r: Any) -> MarketRegime:
    return MarketRegime(
        as_of_date=r.as_of_date,
        regime=r.regime,
        trend_gate_open=bool(r.trend_gate_open),
        days_on_side=int(r.days_on_side),
        spy_close=Decimal(str(r.spy_close)),
        spy_sma200=Decimal(str(r.spy_sma200)),
        spy_sma200_slope_20d=_opt_decimal(r.spy_sma200_slope_20d),
        realized_vol_20d=_opt_decimal(r.realized_vol_20d),
        realized_vol_pct_rank=_opt_decimal(r.realized_vol_pct_rank),
        vol_scalar=_opt_decimal(r.vol_scalar),
        hy_oas_level=_opt_decimal(r.hy_oas_level),
        hy_oas_pct_rank=_opt_decimal(r.hy_oas_pct_rank),
        credit_stress_flag=bool(r.credit_stress_flag),
        methodology_version=int(r.methodology_version),
    )


def upsert_regime(
    as_of: date,
    *,
    trend_gate_open: bool,
    days_on_side: int,
    spy_close: float,
    spy_sma200: float,
    spy_sma200_slope_20d: float | None,
    realized_vol_20d: float | None,
    realized_vol_pct_rank: float | None,
    vol_scalar: float | None,
    hy_oas_level: float | None,
    hy_oas_pct_rank: float | None,
    credit_stress_flag: bool,
    session: Session | None = None,
) -> MarketRegime:
    row = MarketRegime(
        as_of_date=as_of,
        regime=regime_label(
            trend_gate_open=trend_gate_open, credit_stress_flag=credit_stress_flag
        ),
        trend_gate_open=trend_gate_open,
        days_on_side=days_on_side,
        spy_close=Decimal(str(round(spy_close, 4))),
        spy_sma200=Decimal(str(round(spy_sma200, 4))),
        spy_sma200_slope_20d=_opt_round(spy_sma200_slope_20d, 6),
        realized_vol_20d=_opt_round(realized_vol_20d, 6),
        realized_vol_pct_rank=_opt_round(realized_vol_pct_rank, 4),
        vol_scalar=_opt_round(vol_scalar, 4),
        hy_oas_level=_opt_round(hy_oas_level, 4),
        hy_oas_pct_rank=_opt_round(hy_oas_pct_rank, 4),
        credit_stress_flag=credit_stress_flag,
    )
    params = {
        "d": as_of,
        "regime": row.regime,
        "gate_open": row.trend_gate_open,
        "days_on_side": row.days_on_side,
        "close": row.spy_close,
        "sma200": row.spy_sma200,
        "sma_slope": row.spy_sma200_slope_20d,
        "rv": row.realized_vol_20d,
        "rv_rank": row.realized_vol_pct_rank,
        "vol_scalar": row.vol_scalar,
        "hy_oas": row.hy_oas_level,
        "hy_rank": row.hy_oas_pct_rank,
        "stress": row.credit_stress_flag,
        "methver": row.methodology_version,
    }

    def _run(s: Session) -> MarketRegime:
        s.execute(_UPSERT_SQL, params)
        return row

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)


def get_regime(as_of: date, *, session: Session | None = None) -> MarketRegime | None:
    """The stored regime for ``as_of``, or None if not yet computed."""

    def _run(s: Session) -> MarketRegime | None:
        r = s.execute(_GET_SQL, {"d": as_of}).first()
        return _row(r) if r else None

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)


def latest_regime(*, session: Session | None = None) -> MarketRegime | None:
    """The most recently stored regime (any date), or None."""

    def _run(s: Session) -> MarketRegime | None:
        r = s.execute(_LATEST_SQL).first()
        return _row(r) if r else None

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)
