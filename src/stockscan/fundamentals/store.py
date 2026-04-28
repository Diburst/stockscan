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
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from stockscan.db import session_scope


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


def _extract_columns(payload: dict[str, Any]) -> dict[str, Any]:
    """Pull our typed columns out of the EODHD fundamentals payload.

    Field paths verified against EODHD's documented response shape; missing
    fields silently become None (we'd rather have a partial row than fail).
    """
    return {
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
