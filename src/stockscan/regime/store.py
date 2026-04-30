"""Persistence layer for the market_regime table.

The table itself was introduced in migration 0008 (legacy ADX/SMA fields
only). Migration 0010 added the v2 composite columns: per-component
scores, underlying levels, the credit-stress flag, and a
``methodology_version`` discriminator.

This module's contract:

  * :class:`MarketRegime` carries both the legacy fields (always present)
    and the v2 fields (Optional — populated when ``detect_regime`` was
    able to compute them, NULL when a data source was degraded).
  * :func:`upsert_regime` accepts the legacy positional fields plus
    keyword-only optional v2 fields. Callers in v1 code paths continue to
    work unchanged and stamp ``methodology_version=1``; v2 callers pass
    the composite components and stamp ``methodology_version=2``.
  * :func:`get_regime` and :func:`latest_regime` always return the full
    dataclass; v1 rows surface as a MarketRegime whose v2 fields are all
    None.
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

RegimeLabel = Literal["trending_up", "trending_down", "choppy", "transitioning"]


@dataclass(frozen=True, slots=True)
class MarketRegime:
    """Daily snapshot of the regime classifier.

    Legacy fields (always present):
      * ``as_of_date``, ``regime``, ``adx``, ``spy_close``, ``spy_sma200``

    v2 composite fields (Optional, may be ``None`` for legacy rows or
    when the underlying data source was unavailable on the day of
    detection):
      * Component scores in [0, 1]: ``composite_score``, ``vol_score``,
        ``trend_score``, ``breadth_score``, ``credit_score``.
      * Underlying levels: ``vix_level``, ``vix_pct_rank``,
        ``hy_oas_level``, ``hy_oas_pct_rank``, ``hy_oas_zscore``.

    Always present (with sensible defaults for legacy rows):
      * ``credit_stress_flag``: tail-risk circuit breaker (default False).
      * ``methodology_version``: 1 = legacy ADX/SMA only, 2 = composite.
    """

    as_of_date: date
    regime: RegimeLabel
    adx: Decimal
    spy_close: Decimal
    spy_sma200: Decimal

    # ---- v2 component scores (in [0, 1]) ----
    composite_score: Decimal | None = None
    vol_score: Decimal | None = None
    trend_score: Decimal | None = None
    breadth_score: Decimal | None = None
    credit_score: Decimal | None = None

    # ---- v2 underlying levels ----
    vix_level: Decimal | None = None
    vix_pct_rank: Decimal | None = None
    hy_oas_level: Decimal | None = None
    hy_oas_pct_rank: Decimal | None = None
    hy_oas_zscore: Decimal | None = None

    # ---- v2 intermediate signals (added in migration 0011) ----
    # Persisted so the dashboard can show "what we computed most recently"
    # for trend (slope) and breadth (ratio + 20d-vs-200d gap), which would
    # otherwise be hidden inside the aggregated *_score values.
    spy_sma200_slope_20d: Decimal | None = None
    rsp_spy_ratio: Decimal | None = None
    breadth_rel_gap: Decimal | None = None

    # ---- always-present v2 fields ----
    credit_stress_flag: bool = False
    methodology_version: int = 1


# ----------------------------------------------------------------------
# SQL
# ----------------------------------------------------------------------
_SELECT_COLUMNS = (
    "as_of_date, regime, adx, spy_close, spy_sma200, "
    "composite_score, vol_score, trend_score, breadth_score, credit_score, "
    "vix_level, vix_pct_rank, hy_oas_level, hy_oas_pct_rank, hy_oas_zscore, "
    "spy_sma200_slope_20d, rsp_spy_ratio, breadth_rel_gap, "
    "credit_stress_flag, methodology_version"
)


_UPSERT_SQL = text(
    """
    INSERT INTO market_regime (
        as_of_date, regime, adx, spy_close, spy_sma200,
        composite_score, vol_score, trend_score, breadth_score, credit_score,
        vix_level, vix_pct_rank, hy_oas_level, hy_oas_pct_rank, hy_oas_zscore,
        spy_sma200_slope_20d, rsp_spy_ratio, breadth_rel_gap,
        credit_stress_flag, methodology_version
    ) VALUES (
        :d, :r, :adx, :close, :sma200,
        :composite, :vol, :trend, :breadth, :credit,
        :vix, :vix_rank, :hy_oas, :hy_rank, :hy_z,
        :sma_slope, :rsp_ratio, :breadth_gap,
        :stress, :methver
    )
    ON CONFLICT (as_of_date) DO UPDATE SET
        regime               = EXCLUDED.regime,
        adx                  = EXCLUDED.adx,
        spy_close            = EXCLUDED.spy_close,
        spy_sma200           = EXCLUDED.spy_sma200,
        composite_score      = EXCLUDED.composite_score,
        vol_score            = EXCLUDED.vol_score,
        trend_score          = EXCLUDED.trend_score,
        breadth_score        = EXCLUDED.breadth_score,
        credit_score         = EXCLUDED.credit_score,
        vix_level            = EXCLUDED.vix_level,
        vix_pct_rank         = EXCLUDED.vix_pct_rank,
        hy_oas_level         = EXCLUDED.hy_oas_level,
        hy_oas_pct_rank      = EXCLUDED.hy_oas_pct_rank,
        hy_oas_zscore        = EXCLUDED.hy_oas_zscore,
        spy_sma200_slope_20d = EXCLUDED.spy_sma200_slope_20d,
        rsp_spy_ratio        = EXCLUDED.rsp_spy_ratio,
        breadth_rel_gap      = EXCLUDED.breadth_rel_gap,
        credit_stress_flag   = EXCLUDED.credit_stress_flag,
        methodology_version  = EXCLUDED.methodology_version,
        computed_at          = NOW();
    """
)

_GET_SQL = text(f"SELECT {_SELECT_COLUMNS} FROM market_regime WHERE as_of_date = :d")

_LATEST_SQL = text(f"SELECT {_SELECT_COLUMNS} FROM market_regime ORDER BY as_of_date DESC LIMIT 1")


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def _opt_decimal(v: object) -> Decimal | None:
    """Convert a DB-returned NUMERIC value to ``Decimal | None``."""
    return Decimal(str(v)) if v is not None else None


def _opt_round(value: float | None, places: int) -> Decimal | None:
    """Round a float to ``places`` decimal places, preserving None."""
    if value is None:
        return None
    return Decimal(str(round(value, places)))


def _row(r: object) -> MarketRegime:
    # ``getattr`` with default for the 0011 columns so this still maps a
    # row from a DB at migration 0010 (before 0011 was applied). The
    # columns won't exist in that case and the row object won't have
    # those attributes; treating them as None is the right fallback.
    return MarketRegime(
        as_of_date=r.as_of_date,  # type: ignore[attr-defined]
        regime=r.regime,  # type: ignore[attr-defined]
        adx=Decimal(str(r.adx)),  # type: ignore[attr-defined]
        spy_close=Decimal(str(r.spy_close)),  # type: ignore[attr-defined]
        spy_sma200=Decimal(str(r.spy_sma200)),  # type: ignore[attr-defined]
        composite_score=_opt_decimal(r.composite_score),  # type: ignore[attr-defined]
        vol_score=_opt_decimal(r.vol_score),  # type: ignore[attr-defined]
        trend_score=_opt_decimal(r.trend_score),  # type: ignore[attr-defined]
        breadth_score=_opt_decimal(r.breadth_score),  # type: ignore[attr-defined]
        credit_score=_opt_decimal(r.credit_score),  # type: ignore[attr-defined]
        vix_level=_opt_decimal(r.vix_level),  # type: ignore[attr-defined]
        vix_pct_rank=_opt_decimal(r.vix_pct_rank),  # type: ignore[attr-defined]
        hy_oas_level=_opt_decimal(r.hy_oas_level),  # type: ignore[attr-defined]
        hy_oas_pct_rank=_opt_decimal(r.hy_oas_pct_rank),  # type: ignore[attr-defined]
        hy_oas_zscore=_opt_decimal(r.hy_oas_zscore),  # type: ignore[attr-defined]
        spy_sma200_slope_20d=_opt_decimal(getattr(r, "spy_sma200_slope_20d", None)),
        rsp_spy_ratio=_opt_decimal(getattr(r, "rsp_spy_ratio", None)),
        breadth_rel_gap=_opt_decimal(getattr(r, "breadth_rel_gap", None)),
        credit_stress_flag=bool(r.credit_stress_flag),  # type: ignore[attr-defined]
        methodology_version=int(r.methodology_version),  # type: ignore[attr-defined]
    )


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------
def upsert_regime(
    as_of: date,
    regime: RegimeLabel,
    adx: float,
    spy_close: float,
    spy_sma200: float,
    *,
    composite_score: float | None = None,
    vol_score: float | None = None,
    trend_score: float | None = None,
    breadth_score: float | None = None,
    credit_score: float | None = None,
    vix_level: float | None = None,
    vix_pct_rank: float | None = None,
    hy_oas_level: float | None = None,
    hy_oas_pct_rank: float | None = None,
    hy_oas_zscore: float | None = None,
    spy_sma200_slope_20d: float | None = None,
    rsp_spy_ratio: float | None = None,
    breadth_rel_gap: float | None = None,
    credit_stress_flag: bool = False,
    methodology_version: int = 1,
    session: Session | None = None,
) -> MarketRegime:
    """Upsert a regime row.

    The legacy 5 positional arguments preserve full back-compat with
    pre-0010 callers — those calls stamp ``methodology_version=1``. v2
    callers (post-0010 ``detect_regime``) pass the composite kwargs and
    stamp ``methodology_version=2`` explicitly. The 0011 intermediate
    signals (``spy_sma200_slope_20d``, ``rsp_spy_ratio``,
    ``breadth_rel_gap``) are also optional — they're surfaced on the
    dashboard but don't drive sizing, so missing values aren't fatal.
    """
    params: dict[str, Any] = {
        "d": as_of,
        "r": regime,
        "adx": round(adx, 2),
        "close": round(spy_close, 4),
        "sma200": round(spy_sma200, 4),
        # 4-decimal precision matches the NUMERIC(6,4) score columns.
        "composite": _opt_round(composite_score, 4),
        "vol": _opt_round(vol_score, 4),
        "trend": _opt_round(trend_score, 4),
        "breadth": _opt_round(breadth_score, 4),
        "credit": _opt_round(credit_score, 4),
        "vix": _opt_round(vix_level, 4),
        "vix_rank": _opt_round(vix_pct_rank, 4),
        "hy_oas": _opt_round(hy_oas_level, 4),
        "hy_rank": _opt_round(hy_oas_pct_rank, 4),
        "hy_z": _opt_round(hy_oas_zscore, 4),
        # 6-decimal precision matches NUMERIC(8,6) / NUMERIC(10,6).
        "sma_slope": _opt_round(spy_sma200_slope_20d, 6),
        "rsp_ratio": _opt_round(rsp_spy_ratio, 6),
        "breadth_gap": _opt_round(breadth_rel_gap, 6),
        "stress": credit_stress_flag,
        "methver": methodology_version,
    }

    def _run(s: Session) -> MarketRegime:
        s.execute(_UPSERT_SQL, params)
        return MarketRegime(
            as_of_date=as_of,
            regime=regime,
            adx=Decimal(str(round(adx, 2))),
            spy_close=Decimal(str(round(spy_close, 4))),
            spy_sma200=Decimal(str(round(spy_sma200, 4))),
            composite_score=_opt_round(composite_score, 4),
            vol_score=_opt_round(vol_score, 4),
            trend_score=_opt_round(trend_score, 4),
            breadth_score=_opt_round(breadth_score, 4),
            credit_score=_opt_round(credit_score, 4),
            vix_level=_opt_round(vix_level, 4),
            vix_pct_rank=_opt_round(vix_pct_rank, 4),
            hy_oas_level=_opt_round(hy_oas_level, 4),
            hy_oas_pct_rank=_opt_round(hy_oas_pct_rank, 4),
            hy_oas_zscore=_opt_round(hy_oas_zscore, 4),
            spy_sma200_slope_20d=_opt_round(spy_sma200_slope_20d, 6),
            rsp_spy_ratio=_opt_round(rsp_spy_ratio, 6),
            breadth_rel_gap=_opt_round(breadth_rel_gap, 6),
            credit_stress_flag=credit_stress_flag,
            methodology_version=methodology_version,
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
