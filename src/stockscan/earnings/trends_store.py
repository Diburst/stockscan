"""Persistence + queries for the ``earnings_trends`` table (migration 0018)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as _date
from typing import TYPE_CHECKING, Any

from sqlalchemy import text

from stockscan.db import session_scope

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


@dataclass(frozen=True, slots=True)
class EarningsTrend:
    """One row from ``earnings_trends`` — one (symbol, period) point."""

    trend_id: int
    symbol: str
    period_end: _date
    period: str  # '0q' | '+1q' | '0y' | '+1y'

    eps_estimate_avg: float | None
    eps_estimate_low: float | None
    eps_estimate_high: float | None
    eps_year_ago: float | None
    eps_growth: float | None
    eps_analyst_count: int | None

    rev_estimate_avg: float | None
    rev_estimate_low: float | None
    rev_estimate_high: float | None
    rev_year_ago: float | None
    rev_growth: float | None
    rev_analyst_count: int | None

    eps_trend_current: float | None
    eps_trend_7d_ago: float | None
    eps_trend_30d_ago: float | None
    eps_trend_60d_ago: float | None
    eps_trend_90d_ago: float | None

    eps_revisions_up_7d: int | None
    eps_revisions_up_30d: int | None
    eps_revisions_down_30d: int | None

    @property
    def net_revisions_30d(self) -> int | None:
        """Up − down over the last 30 days. The single most-actionable
        revision-drift signal: positive = consensus walking higher."""
        up = self.eps_revisions_up_30d
        down = self.eps_revisions_down_30d
        if up is None and down is None:
            return None
        return (up or 0) - (down or 0)

    @property
    def trend_30d_change_pct(self) -> float | None:
        """Percent change in consensus EPS over the last 30 days.
        Captures the magnitude of the revision walk, not just direction."""
        cur = self.eps_trend_current
        old = self.eps_trend_30d_ago
        if cur is None or old is None or old == 0:
            return None
        return (cur - old) / abs(old) * 100.0


# Stringified-number tolerance — EODHD returns trend values as JSON
# strings ("7.9816") rather than numerics. Coerce here so the store
# layer can call ``float(...)`` without surprises later.
def _flt(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v: Any) -> int | None:
    if v is None or v == "":
        return None
    try:
        # EODHD passes "40.0000" for counts — cast through float first.
        return int(float(v))
    except (TypeError, ValueError):
        return None


_UPSERT_SQL = text(
    """
    INSERT INTO earnings_trends (
        symbol, period_end, period,
        eps_estimate_avg, eps_estimate_low, eps_estimate_high,
        eps_year_ago, eps_growth, eps_analyst_count,
        rev_estimate_avg, rev_estimate_low, rev_estimate_high,
        rev_year_ago, rev_growth, rev_analyst_count,
        eps_trend_current, eps_trend_7d_ago, eps_trend_30d_ago,
        eps_trend_60d_ago, eps_trend_90d_ago,
        eps_revisions_up_7d, eps_revisions_up_30d, eps_revisions_down_30d
    ) VALUES (
        :symbol, :period_end, :period,
        :eps_estimate_avg, :eps_estimate_low, :eps_estimate_high,
        :eps_year_ago, :eps_growth, :eps_analyst_count,
        :rev_estimate_avg, :rev_estimate_low, :rev_estimate_high,
        :rev_year_ago, :rev_growth, :rev_analyst_count,
        :eps_trend_current, :eps_trend_7d_ago, :eps_trend_30d_ago,
        :eps_trend_60d_ago, :eps_trend_90d_ago,
        :eps_revisions_up_7d, :eps_revisions_up_30d, :eps_revisions_down_30d
    )
    ON CONFLICT (symbol, period_end, period) DO UPDATE SET
        eps_estimate_avg       = EXCLUDED.eps_estimate_avg,
        eps_estimate_low       = EXCLUDED.eps_estimate_low,
        eps_estimate_high      = EXCLUDED.eps_estimate_high,
        eps_year_ago           = EXCLUDED.eps_year_ago,
        eps_growth             = EXCLUDED.eps_growth,
        eps_analyst_count      = EXCLUDED.eps_analyst_count,
        rev_estimate_avg       = EXCLUDED.rev_estimate_avg,
        rev_estimate_low       = EXCLUDED.rev_estimate_low,
        rev_estimate_high      = EXCLUDED.rev_estimate_high,
        rev_year_ago           = EXCLUDED.rev_year_ago,
        rev_growth             = EXCLUDED.rev_growth,
        rev_analyst_count      = EXCLUDED.rev_analyst_count,
        eps_trend_current      = EXCLUDED.eps_trend_current,
        eps_trend_7d_ago       = EXCLUDED.eps_trend_7d_ago,
        eps_trend_30d_ago      = EXCLUDED.eps_trend_30d_ago,
        eps_trend_60d_ago      = EXCLUDED.eps_trend_60d_ago,
        eps_trend_90d_ago      = EXCLUDED.eps_trend_90d_ago,
        eps_revisions_up_7d    = EXCLUDED.eps_revisions_up_7d,
        eps_revisions_up_30d   = EXCLUDED.eps_revisions_up_30d,
        eps_revisions_down_30d = EXCLUDED.eps_revisions_down_30d,
        fetched_at             = NOW()
    """
)


def upsert_trends(
    records_by_symbol: dict[str, list[dict[str, Any]]],
    *,
    session: Session | None = None,
) -> int:
    """Upsert the provider's grouped trend records.

    Each value in ``records_by_symbol`` is the list of dated trend points
    returned by EODHD for that symbol — different `period` slots covering
    current quarter / next quarter / current year / next year.
    """
    payload: list[dict[str, Any]] = []
    for symbol, records in records_by_symbol.items():
        for r in records or []:
            raw_end = r.get("date")
            period = r.get("period")
            if not raw_end or not period:
                continue
            try:
                period_end = _date.fromisoformat(raw_end)
            except ValueError:
                continue
            payload.append({
                "symbol": symbol,
                "period_end": period_end,
                "period": period,
                "eps_estimate_avg": _flt(r.get("earningsEstimateAvg")),
                "eps_estimate_low": _flt(r.get("earningsEstimateLow")),
                "eps_estimate_high": _flt(r.get("earningsEstimateHigh")),
                "eps_year_ago": _flt(r.get("earningsEstimateYearAgoEps")),
                "eps_growth": _flt(r.get("earningsEstimateGrowth")),
                "eps_analyst_count": _int(r.get("earningsEstimateNumberOfAnalysts")),
                "rev_estimate_avg": _flt(r.get("revenueEstimateAvg")),
                "rev_estimate_low": _flt(r.get("revenueEstimateLow")),
                "rev_estimate_high": _flt(r.get("revenueEstimateHigh")),
                "rev_year_ago": _flt(r.get("revenueEstimateYearAgoEps")),
                "rev_growth": _flt(r.get("revenueEstimateGrowth")),
                "rev_analyst_count": _int(r.get("revenueEstimateNumberOfAnalysts")),
                "eps_trend_current": _flt(r.get("epsTrendCurrent")),
                "eps_trend_7d_ago": _flt(r.get("epsTrend7daysAgo")),
                "eps_trend_30d_ago": _flt(r.get("epsTrend30daysAgo")),
                "eps_trend_60d_ago": _flt(r.get("epsTrend60daysAgo")),
                "eps_trend_90d_ago": _flt(r.get("epsTrend90daysAgo")),
                "eps_revisions_up_7d": _int(r.get("epsRevisionsUpLast7days")),
                "eps_revisions_up_30d": _int(r.get("epsRevisionsUpLast30days")),
                "eps_revisions_down_30d": _int(r.get("epsRevisionsDownLast30days")),
            })

    if not payload:
        return 0

    def _run(s: Session) -> int:
        result = s.execute(_UPSERT_SQL, payload)
        return result.rowcount or len(payload)

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)


_SELECT_SQL = text(
    """
    SELECT trend_id, symbol, period_end, period,
           eps_estimate_avg, eps_estimate_low, eps_estimate_high,
           eps_year_ago, eps_growth, eps_analyst_count,
           rev_estimate_avg, rev_estimate_low, rev_estimate_high,
           rev_year_ago, rev_growth, rev_analyst_count,
           eps_trend_current, eps_trend_7d_ago, eps_trend_30d_ago,
           eps_trend_60d_ago, eps_trend_90d_ago,
           eps_revisions_up_7d, eps_revisions_up_30d, eps_revisions_down_30d
    FROM earnings_trends
    WHERE symbol = :symbol
      -- ``since`` is optional; when NULL we return every trend row for
      -- the symbol (used by backtest-style analysis). The CAST is the
      -- same type-pinning trick that fixed AmbiguousParameter on the
      -- macro-events query: bare bind params on the IS NULL side of an
      -- OR carry no type info at prepare time.
      AND (CAST(:since AS DATE) IS NULL OR period_end >= :since)
    -- DESC so the most forward-looking estimates render first — that's
    -- the actionable order for the analysis page table. The period
    -- secondary sort is a deterministic tiebreak; in practice no two
    -- rows for the same symbol share both period_end AND period because
    -- of the natural-key UNIQUE constraint.
    ORDER BY period_end DESC, period
    """
)


def _row_to_trend(r: Any) -> EarningsTrend:
    return EarningsTrend(
        trend_id=int(r.trend_id),
        symbol=r.symbol,
        period_end=r.period_end,
        period=r.period,
        eps_estimate_avg=float(r.eps_estimate_avg) if r.eps_estimate_avg is not None else None,
        eps_estimate_low=float(r.eps_estimate_low) if r.eps_estimate_low is not None else None,
        eps_estimate_high=float(r.eps_estimate_high) if r.eps_estimate_high is not None else None,
        eps_year_ago=float(r.eps_year_ago) if r.eps_year_ago is not None else None,
        eps_growth=float(r.eps_growth) if r.eps_growth is not None else None,
        eps_analyst_count=int(r.eps_analyst_count) if r.eps_analyst_count is not None else None,
        rev_estimate_avg=float(r.rev_estimate_avg) if r.rev_estimate_avg is not None else None,
        rev_estimate_low=float(r.rev_estimate_low) if r.rev_estimate_low is not None else None,
        rev_estimate_high=float(r.rev_estimate_high) if r.rev_estimate_high is not None else None,
        rev_year_ago=float(r.rev_year_ago) if r.rev_year_ago is not None else None,
        rev_growth=float(r.rev_growth) if r.rev_growth is not None else None,
        rev_analyst_count=int(r.rev_analyst_count) if r.rev_analyst_count is not None else None,
        eps_trend_current=float(r.eps_trend_current) if r.eps_trend_current is not None else None,
        eps_trend_7d_ago=float(r.eps_trend_7d_ago) if r.eps_trend_7d_ago is not None else None,
        eps_trend_30d_ago=float(r.eps_trend_30d_ago) if r.eps_trend_30d_ago is not None else None,
        eps_trend_60d_ago=float(r.eps_trend_60d_ago) if r.eps_trend_60d_ago is not None else None,
        eps_trend_90d_ago=float(r.eps_trend_90d_ago) if r.eps_trend_90d_ago is not None else None,
        eps_revisions_up_7d=int(r.eps_revisions_up_7d) if r.eps_revisions_up_7d is not None else None,
        eps_revisions_up_30d=int(r.eps_revisions_up_30d) if r.eps_revisions_up_30d is not None else None,
        eps_revisions_down_30d=int(r.eps_revisions_down_30d) if r.eps_revisions_down_30d is not None else None,
    )


def latest_trend(
    symbol: str,
    *,
    since: _date | None = None,
    session: Session | None = None,
) -> list[EarningsTrend]:
    """Every trend point we have for ``symbol`` whose period_end is on
    or after ``since`` (when supplied), in DESCENDING period_end order
    so the most forward-looking rows come first.

    Pass ``since=None`` to get every row regardless of age — useful for
    historical / backtest analysis. UI callers pass ``today - 365 days``
    so the table doesn't surface estimates for quarters that ended a
    decade ago.
    """

    def _run(s: Session) -> list[EarningsTrend]:
        rows = s.execute(_SELECT_SQL, {"symbol": symbol, "since": since}).all()
        return [_row_to_trend(r) for r in rows]

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)


def revision_summary(
    symbol: str,
    *,
    since: _date | None = None,
    session: Session | None = None,
) -> EarningsTrend | None:
    """The single most-actionable trend row for ``symbol``.

    Prefers the ``+1q`` (next quarter) point because that's where the
    revision dynamics tend to be sharpest for swing-horizon strategies.
    Falls back to ``0q`` (current quarter) or any other available period
    if next quarter isn't on file.

    When multiple snapshots share the same ``period`` label (EODHD
    occasionally publishes historical ``+1q`` snapshots that refer to
    quarters that have since closed), the tiebreak picks the LATEST
    ``period_end`` — i.e., the most current forward-looking estimate
    rather than a stale historical one.
    """
    trends = latest_trend(symbol, since=since, session=session)
    if not trends:
        return None
    priority = {"+1q": 0, "0q": 1, "+1y": 2, "0y": 3}
    # ``min`` with a tuple key. We want lowest priority first, but
    # within the same priority bucket we want the LATEST period_end —
    # so negate the ordinal so "larger date" sorts first under ``min``.
    return min(
        trends,
        key=lambda t: (priority.get(t.period, 99), -t.period_end.toordinal()),
    )
