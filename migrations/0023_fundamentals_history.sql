-- 0023_fundamentals_history.sql
--
-- Point-in-time shares outstanding, for accurate historical cap-weighting.
--
-- `fundamentals_snapshot` only keeps the *latest* shares_outstanding, which is
-- fine for screening but wrong for a historical cap-weighted composite: it would
-- retroactively give a stock its current size across its whole past. The full
-- EODHD `/fundamentals` payload we already store in `raw_payload` carries a
-- quarterly/annual `outstandingShares` history (and a Balance_Sheet fallback),
-- so we can reconstruct point-in-time market cap = shares(t) x price(t) with no
-- extra API calls — we just extract what's already in the blob.
--
-- This table is the time-series sibling of `fundamentals_snapshot`: one row per
-- (symbol, reporting period). It mirrors the existing "extract typed columns
-- from the blob" pattern, just per-period instead of latest-only. The composite
-- builder forward-fills these step values onto the daily trading calendar.

CREATE TABLE fundamentals_history (
    symbol              TEXT   NOT NULL,
    period_date         DATE   NOT NULL,          -- reporting-period end date
    shares_outstanding  BIGINT NOT NULL,          -- as-reported (un-split-adjusted)
    period_kind         TEXT   NOT NULL DEFAULT 'quarterly',  -- 'quarterly' | 'annual'
    source              TEXT   NOT NULL DEFAULT 'eodhd',
    fetched_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (symbol, period_date)
);

-- Primary access pattern: "give me every share-count point for this symbol,
-- oldest first" so the builder can forward-fill onto the price index.
CREATE INDEX idx_fundamentals_history_symbol ON fundamentals_history (symbol, period_date);
