-- 0005_fundamentals.sql
--
-- Latest-snapshot fundamentals per symbol. We extract a handful of
-- frequently-used fields into typed columns (for fast WHERE/ORDER-BY at
-- scan time), and stash the entire EODHD response in raw_payload JSONB
-- so any future field is reachable without a schema migration.
--
-- Refresh policy: the EODHD /fundamentals endpoint costs one API call per
-- symbol. Most fields change quarterly with earnings; market_cap drifts
-- daily with price. A weekly refresh is a reasonable default; daily is
-- fine if you want market_cap perfectly current.

CREATE TABLE fundamentals_snapshot (
    symbol             TEXT PRIMARY KEY,

    -- Identity
    name               TEXT,
    sector             TEXT,
    industry           TEXT,
    country            TEXT,
    currency           TEXT,
    exchange           TEXT,
    isin               TEXT,
    ipo_date           DATE,

    -- Highlights / valuation
    market_cap         NUMERIC(20, 2),
    shares_outstanding BIGINT,
    shares_float       BIGINT,
    pe_ratio           NUMERIC(12, 4),
    forward_pe         NUMERIC(12, 4),
    peg_ratio          NUMERIC(12, 4),
    eps_ttm            NUMERIC(12, 4),
    eps_estimate_cy    NUMERIC(12, 4),
    book_value         NUMERIC(14, 4),
    price_to_book      NUMERIC(12, 4),
    price_to_sales_ttm NUMERIC(12, 4),
    profit_margin      NUMERIC(8, 6),
    operating_margin   NUMERIC(8, 6),
    return_on_equity   NUMERIC(8, 6),
    return_on_assets   NUMERIC(8, 6),
    revenue_ttm        NUMERIC(20, 2),
    revenue_per_share_ttm NUMERIC(14, 4),
    gross_profit_ttm   NUMERIC(20, 2),
    ebitda             NUMERIC(20, 2),
    debt_to_equity     NUMERIC(12, 4),

    -- Dividends
    dividend_yield     NUMERIC(8, 6),
    dividend_share     NUMERIC(10, 4),
    payout_ratio       NUMERIC(8, 6),

    -- Technical (from EODHD; we still compute our own from bars)
    beta               NUMERIC(8, 4),
    week_52_high       NUMERIC(14, 4),
    week_52_low        NUMERIC(14, 4),
    day_50_ma          NUMERIC(14, 4),
    day_200_ma         NUMERIC(14, 4),

    -- Analyst ratings
    analyst_rating     NUMERIC(4, 2),
    analyst_target     NUMERIC(14, 4),
    analyst_count      INTEGER,

    -- Full payload for anything else (financial statements, holders, ESG, ...)
    raw_payload        JSONB NOT NULL,
    fetched_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Indexes for the queries we expect strategies + UI to make.
CREATE INDEX idx_fundamentals_market_cap
    ON fundamentals_snapshot (market_cap DESC)
    WHERE market_cap IS NOT NULL;
CREATE INDEX idx_fundamentals_sector
    ON fundamentals_snapshot (sector)
    WHERE sector IS NOT NULL;
