"""CRUD + helpers for fundamentals_snapshot.

Frequently-used fields are extracted into typed columns at upsert time
(see `_extract_columns`), so SQL filters like
"market_cap >= 80th percentile of S&P 500" run against an indexed column
rather than scanning a JSONB blob.

The `raw_payload` column keeps everything else (financial statements,
ESG scores, holders, etc.) for later extraction without a migration.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from stockscan.db import session_scope

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Domain dataclass
# ---------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Fundamentals:
    symbol: str
    name: str | None
    sector: str | None
    industry: str | None
    market_cap: Decimal | None
    shares_outstanding: int | None
    pe_ratio: Decimal | None
    forward_pe: Decimal | None
    eps_ttm: Decimal | None
    dividend_yield: Decimal | None
    beta: Decimal | None
    week_52_high: Decimal | None
    week_52_low: Decimal | None
    fetched_at: datetime


def _row_to_fundamentals(r: Any) -> Fundamentals:
    def _dec(v: Any) -> Decimal | None:
        return Decimal(str(v)) if v is not None else None

    return Fundamentals(
        symbol=r.symbol,
        name=r.name,
        sector=r.sector,
        industry=r.industry,
        market_cap=_dec(r.market_cap),
        shares_outstanding=int(r.shares_outstanding) if r.shares_outstanding else None,
        pe_ratio=_dec(r.pe_ratio),
        forward_pe=_dec(r.forward_pe),
        eps_ttm=_dec(r.eps_ttm),
        dividend_yield=_dec(r.dividend_yield),
        beta=_dec(r.beta),
        week_52_high=_dec(r.week_52_high),
        week_52_low=_dec(r.week_52_low),
        fetched_at=r.fetched_at,
    )


# ---------------------------------------------------------------------
# Field extraction from EODHD payload shape
# ---------------------------------------------------------------------
def _g(obj: Any, *keys: str) -> Any:
    """Safe nested .get — returns None on any missing/None segment."""
    cur = obj
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
        if cur is None:
            return None
    return cur


def _to_number(v: Any) -> float | None:
    """Convert a possibly-string number to float; '0', '', 'NA' → None."""
    if v is None or v == "" or v == "NA":
        return None
    try:
        out = float(v)
    except (TypeError, ValueError):
        return None
    return out if out == out else None  # filter NaN


def _to_int(v: Any) -> int | None:
    n = _to_number(v)
    return int(n) if n is not None else None


def _to_date(v: Any) -> date | None:
    if not isinstance(v, str) or not v:
        return None
    try:
        return date.fromisoformat(v[:10])
    except ValueError:
        return None


# ---------------------------------------------------------------------
# Defensive numeric coercion — keep one bad field from aborting a row
# ---------------------------------------------------------------------
# (precision, scale) for every NUMERIC column. MUST mirror the schema
# (migrations 0005 + 0014). A NUMERIC(p, s) holds |value| < 10**(p - s); the
# provider occasionally returns values past that (e.g. payout_ratio = 153.75,
# extreme margins/ROE on tiny-revenue names). Before 0014 + this guard, such a
# value raised psycopg's NumericValueOutOfRange and aborted the *entire* INSERT,
# so the symbol got no row at all — no sector, no market cap, dropped from the
# sector composites. We now coerce out-of-range / non-finite numbers to NULL,
# which is exactly the "missing field" semantics used everywhere else (callers
# abstain on None rather than acting on a bogus value).
_INT_MAX = 2**31 - 1  # INTEGER column ceiling (analyst_count)
_BIGINT_MAX = 2**63 - 1  # BIGINT column ceiling (shares_*)

_NUMERIC_PRECISION: dict[str, tuple[int, int]] = {
    "market_cap": (20, 2),
    "pe_ratio": (12, 4),
    "forward_pe": (12, 4),
    "peg_ratio": (12, 4),
    "eps_ttm": (12, 4),
    "eps_estimate_cy": (12, 4),
    "book_value": (14, 4),
    "price_to_book": (12, 4),
    "price_to_sales_ttm": (12, 4),
    "profit_margin": (12, 6),  # widened from (8,6) in migration 0014
    "operating_margin": (12, 6),
    "return_on_equity": (12, 6),
    "return_on_assets": (12, 6),
    "revenue_ttm": (20, 2),
    "revenue_per_share_ttm": (14, 4),
    "gross_profit_ttm": (20, 2),
    "ebitda": (20, 2),
    "debt_to_equity": (12, 4),
    "dividend_yield": (12, 6),
    "dividend_share": (10, 4),
    "payout_ratio": (12, 6),
    "beta": (8, 4),
    "week_52_high": (14, 4),
    "week_52_low": (14, 4),
    "day_50_ma": (14, 4),
    "day_200_ma": (14, 4),
    "analyst_rating": (4, 2),
    "analyst_target": (14, 4),
}


def _fit_numeric(value: Any, precision: int, scale: int, *, col: str) -> float | None:
    """Coerce ``value`` to fit ``NUMERIC(precision, scale)``, else return None.

    Returns None — never raises — for None, non-numeric, non-finite (NaN/±inf),
    or magnitudes at/beyond the column's integer-part capacity. Otherwise rounds
    to ``scale`` (Postgres would round the fractional part anyway; the overflow
    error is only ever about the integer part).
    """
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        log.warning("fundamentals: %s=%r is non-finite; storing NULL", col, value)
        return None
    limit = 10 ** (precision - scale)  # exclusive integer-part bound
    if abs(v) >= limit:
        log.warning(
            "fundamentals: %s=%r exceeds NUMERIC(%d,%d) capacity; storing NULL",
            col, v, precision, scale,
        )
        return None
    return round(v, scale)


def _fit_int(value: Any, max_abs: int, *, col: str) -> int | None:
    """Coerce an integer to fit its column, else None (never raises)."""
    if value is None:
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    if abs(n) > max_abs:
        log.warning("fundamentals: %s=%r exceeds column capacity; storing NULL", col, n)
        return None
    return n


def _extract_columns(payload: dict[str, Any]) -> dict[str, Any]:
    """Pull our typed columns out of the EODHD fundamentals payload.

    Field paths verified against EODHD's documented response shape; missing
    fields silently become None (we'd rather have a partial row than fail).
    """
    cols: dict[str, Any] = {
        "symbol":              _g(payload, "General", "Code") or _g(payload, "Code"),
        "name":                _g(payload, "General", "Name"),
        "sector":              _g(payload, "General", "Sector"),
        "industry":            _g(payload, "General", "Industry"),
        "country":             _g(payload, "General", "CountryName") or _g(payload, "General", "Country"),
        "currency":            _g(payload, "General", "CurrencyCode"),
        "exchange":            _g(payload, "General", "Exchange"),
        "isin":                _g(payload, "General", "ISIN"),
        "ipo_date":            _to_date(_g(payload, "General", "IPODate")),

        "market_cap":          _to_number(_g(payload, "Highlights", "MarketCapitalization")),
        "shares_outstanding":  _to_int(_g(payload, "SharesStats", "SharesOutstanding")),
        "shares_float":        _to_int(_g(payload, "SharesStats", "SharesFloat")),
        "pe_ratio":            _to_number(_g(payload, "Highlights", "PERatio")),
        "forward_pe":          _to_number(_g(payload, "Valuation", "ForwardPE")),
        "peg_ratio":           _to_number(_g(payload, "Highlights", "PEGRatio")),
        "eps_ttm":             _to_number(_g(payload, "Highlights", "EarningsShare")),
        "eps_estimate_cy":     _to_number(_g(payload, "Highlights", "EPSEstimateCurrentYear")),
        "book_value":          _to_number(_g(payload, "Highlights", "BookValue")),
        "price_to_book":       _to_number(_g(payload, "Valuation", "PriceBookMRQ")),
        "price_to_sales_ttm":  _to_number(_g(payload, "Valuation", "PriceSalesTTM")),
        "profit_margin":       _to_number(_g(payload, "Highlights", "ProfitMargin")),
        "operating_margin":    _to_number(_g(payload, "Highlights", "OperatingMarginTTM")),
        "return_on_equity":    _to_number(_g(payload, "Highlights", "ReturnOnEquityTTM")),
        "return_on_assets":    _to_number(_g(payload, "Highlights", "ReturnOnAssetsTTM")),
        "revenue_ttm":         _to_number(_g(payload, "Highlights", "RevenueTTM")),
        "revenue_per_share_ttm": _to_number(_g(payload, "Highlights", "RevenuePerShareTTM")),
        "gross_profit_ttm":    _to_number(_g(payload, "Highlights", "GrossProfitTTM")),
        "ebitda":              _to_number(_g(payload, "Highlights", "EBITDA")),
        "debt_to_equity":      _to_number(_g(payload, "Highlights", "DebtToEquity")),

        "dividend_yield":      _to_number(_g(payload, "Highlights", "DividendYield")),
        "dividend_share":      _to_number(_g(payload, "Highlights", "DividendShare")),
        "payout_ratio":        _to_number(_g(payload, "SplitsDividends", "PayoutRatio")),

        "beta":                _to_number(_g(payload, "Technicals", "Beta")),
        "week_52_high":        _to_number(_g(payload, "Technicals", "52WeekHigh")),
        "week_52_low":         _to_number(_g(payload, "Technicals", "52WeekLow")),
        "day_50_ma":           _to_number(_g(payload, "Technicals", "50DayMA")),
        "day_200_ma":          _to_number(_g(payload, "Technicals", "200DayMA")),

        "analyst_rating":      _to_number(_g(payload, "AnalystRatings", "Rating")),
        "analyst_target":      _to_number(_g(payload, "AnalystRatings", "TargetPrice")),
        "analyst_count":       _to_int(
            sum(_to_int(v) or 0 for v in (_g(payload, "AnalystRatings") or {}).values())
        ),
    }

    # Defensive coercion so a single out-of-range / non-finite field can never
    # abort the whole row (psycopg NumericValueOutOfRange). Out-of-bounds values
    # become NULL — the same "missing" semantics used elsewhere.
    for c, (prec, scale) in _NUMERIC_PRECISION.items():
        cols[c] = _fit_numeric(cols.get(c), prec, scale, col=c)
    cols["analyst_count"] = _fit_int(cols.get("analyst_count"), _INT_MAX, col="analyst_count")
    cols["shares_outstanding"] = _fit_int(
        cols.get("shares_outstanding"), _BIGINT_MAX, col="shares_outstanding"
    )
    cols["shares_float"] = _fit_int(cols.get("shares_float"), _BIGINT_MAX, col="shares_float")
    return cols


# ---------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------
_UPSERT_SQL = text(
    """
    INSERT INTO fundamentals_snapshot (
        symbol, name, sector, industry, country, currency, exchange, isin, ipo_date,
        market_cap, shares_outstanding, shares_float, pe_ratio, forward_pe, peg_ratio,
        eps_ttm, eps_estimate_cy, book_value, price_to_book, price_to_sales_ttm,
        profit_margin, operating_margin, return_on_equity, return_on_assets,
        revenue_ttm, revenue_per_share_ttm, gross_profit_ttm, ebitda, debt_to_equity,
        dividend_yield, dividend_share, payout_ratio,
        beta, week_52_high, week_52_low, day_50_ma, day_200_ma,
        analyst_rating, analyst_target, analyst_count,
        raw_payload, fetched_at
    ) VALUES (
        :symbol, :name, :sector, :industry, :country, :currency, :exchange, :isin, :ipo_date,
        :market_cap, :shares_outstanding, :shares_float, :pe_ratio, :forward_pe, :peg_ratio,
        :eps_ttm, :eps_estimate_cy, :book_value, :price_to_book, :price_to_sales_ttm,
        :profit_margin, :operating_margin, :return_on_equity, :return_on_assets,
        :revenue_ttm, :revenue_per_share_ttm, :gross_profit_ttm, :ebitda, :debt_to_equity,
        :dividend_yield, :dividend_share, :payout_ratio,
        :beta, :week_52_high, :week_52_low, :day_50_ma, :day_200_ma,
        :analyst_rating, :analyst_target, :analyst_count,
        CAST(:raw_payload AS JSONB), NOW()
    )
    ON CONFLICT (symbol) DO UPDATE SET
        name = EXCLUDED.name,
        sector = EXCLUDED.sector,
        industry = EXCLUDED.industry,
        country = EXCLUDED.country,
        currency = EXCLUDED.currency,
        exchange = EXCLUDED.exchange,
        isin = EXCLUDED.isin,
        ipo_date = EXCLUDED.ipo_date,
        market_cap = EXCLUDED.market_cap,
        shares_outstanding = EXCLUDED.shares_outstanding,
        shares_float = EXCLUDED.shares_float,
        pe_ratio = EXCLUDED.pe_ratio,
        forward_pe = EXCLUDED.forward_pe,
        peg_ratio = EXCLUDED.peg_ratio,
        eps_ttm = EXCLUDED.eps_ttm,
        eps_estimate_cy = EXCLUDED.eps_estimate_cy,
        book_value = EXCLUDED.book_value,
        price_to_book = EXCLUDED.price_to_book,
        price_to_sales_ttm = EXCLUDED.price_to_sales_ttm,
        profit_margin = EXCLUDED.profit_margin,
        operating_margin = EXCLUDED.operating_margin,
        return_on_equity = EXCLUDED.return_on_equity,
        return_on_assets = EXCLUDED.return_on_assets,
        revenue_ttm = EXCLUDED.revenue_ttm,
        revenue_per_share_ttm = EXCLUDED.revenue_per_share_ttm,
        gross_profit_ttm = EXCLUDED.gross_profit_ttm,
        ebitda = EXCLUDED.ebitda,
        debt_to_equity = EXCLUDED.debt_to_equity,
        dividend_yield = EXCLUDED.dividend_yield,
        dividend_share = EXCLUDED.dividend_share,
        payout_ratio = EXCLUDED.payout_ratio,
        beta = EXCLUDED.beta,
        week_52_high = EXCLUDED.week_52_high,
        week_52_low = EXCLUDED.week_52_low,
        day_50_ma = EXCLUDED.day_50_ma,
        day_200_ma = EXCLUDED.day_200_ma,
        analyst_rating = EXCLUDED.analyst_rating,
        analyst_target = EXCLUDED.analyst_target,
        analyst_count = EXCLUDED.analyst_count,
        raw_payload = EXCLUDED.raw_payload,
        fetched_at = NOW();
    """
)


def upsert_fundamentals(
    symbol: str,
    payload: dict[str, Any],
    *,
    session: Session | None = None,
) -> None:
    """Extract typed columns from `payload` and upsert."""
    cols = _extract_columns(payload)
    # Ensure symbol matches what we're upserting (caller's symbol takes precedence
    # if the payload has a different code, e.g., for class-A/B share quirks)
    cols["symbol"] = symbol
    cols["raw_payload"] = json.dumps(payload, default=str)

    if session is not None:
        session.execute(_UPSERT_SQL, cols)
        return
    with session_scope() as s:
        s.execute(_UPSERT_SQL, cols)


# ---------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------
def get_fundamentals(symbol: str, *, session: Session | None = None) -> Fundamentals | None:
    sql = text(
        """
        SELECT symbol, name, sector, industry, market_cap, shares_outstanding,
               pe_ratio, forward_pe, eps_ttm, dividend_yield, beta,
               week_52_high, week_52_low, fetched_at
        FROM fundamentals_snapshot WHERE symbol = :s
        """
    )

    def _run(s: Session) -> Fundamentals | None:
        row = s.execute(sql, {"s": symbol}).first()
        return _row_to_fundamentals(row) if row else None

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)


def get_market_cap(symbol: str, *, session: Session | None = None) -> Decimal | None:
    """Fast path: just market cap, no row hydration."""
    sql = text("SELECT market_cap FROM fundamentals_snapshot WHERE symbol = :s")

    def _run(s: Session) -> Decimal | None:
        row = s.execute(sql, {"s": symbol}).first()
        return Decimal(str(row.market_cap)) if row and row.market_cap else None

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)


def market_cap_percentile(
    symbol: str,
    *,
    universe: list[str] | None = None,
    session: Session | None = None,
) -> float | None:
    """Return `symbol`'s market-cap percentile rank within `universe` (or
    the entire fundamentals_snapshot table if universe is None) as a float
    in [0, 100]. Returns None if the symbol has no market cap recorded.

    Percentile = % of symbols at or below this one's market cap.
    100 = largest in the universe. 0 = smallest.
    """
    if universe is not None and symbol not in universe:
        return None

    if universe is None:
        sql = text(
            """
            WITH ranked AS (
                SELECT symbol,
                       PERCENT_RANK() OVER (ORDER BY market_cap) * 100 AS pct
                FROM fundamentals_snapshot
                WHERE market_cap IS NOT NULL
            )
            SELECT pct FROM ranked WHERE symbol = :s
            """
        )
        params: dict[str, object] = {"s": symbol}
    else:
        sql = text(
            """
            WITH ranked AS (
                SELECT symbol,
                       PERCENT_RANK() OVER (ORDER BY market_cap) * 100 AS pct
                FROM fundamentals_snapshot
                WHERE market_cap IS NOT NULL
                  AND symbol = ANY(:u)
            )
            SELECT pct FROM ranked WHERE symbol = :s
            """
        )
        params = {"s": symbol, "u": universe}

    def _run(s: Session) -> float | None:
        row = s.execute(sql, params).first()
        return float(row.pct) if row and row.pct is not None else None

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)


def list_by_market_cap(
    limit: int = 100,
    *,
    session: Session | None = None,
) -> list[Fundamentals]:
    """Top-N symbols by market cap. Used for ranked listings."""
    sql = text(
        """
        SELECT symbol, name, sector, industry, market_cap, shares_outstanding,
               pe_ratio, forward_pe, eps_ttm, dividend_yield, beta,
               week_52_high, week_52_low, fetched_at
        FROM fundamentals_snapshot
        WHERE market_cap IS NOT NULL
        ORDER BY market_cap DESC
        LIMIT :lim
        """
    )

    def _run(s: Session) -> list[Fundamentals]:
        return [_row_to_fundamentals(r) for r in s.execute(sql, {"lim": limit})]

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)
