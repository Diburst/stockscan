-- 0004_technical_scores.sql
--
-- Per-(symbol, date, strategy) technical confirmation scores.
-- Computed by the scanner after persisting each signal (passing or rejected),
-- so analysts can later ask "did weak technicals correlate with rejections?".
-- The watchlist also writes neutral-strategy rows (strategy_name = '_neutral').

CREATE TABLE technical_scores (
    symbol         TEXT         NOT NULL,
    as_of_date     DATE         NOT NULL,
    strategy_name  TEXT         NOT NULL,    -- registered strategy or '_neutral'
    score          NUMERIC(6,4) NOT NULL,    -- in [-1, +1]
    breakdown      JSONB        NOT NULL,    -- per-indicator values + sub-scores
    computed_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (symbol, as_of_date, strategy_name),
    CHECK (score >= -1 AND score <= 1)
);

CREATE INDEX idx_tech_scores_symbol_date
    ON technical_scores (symbol, as_of_date DESC);
