-- 0019_insider_transactions.sql
--
-- SEC Form 4 insider transactions (EODHD /api/insider-transactions).
--
-- Each call costs 10 API credits, so the refresh layer gates with the
-- companion insider_refresh_log table: at most one watchlist-wide pull
-- per 23 hours, with separate per-symbol cooldowns for the analysis
-- page's on-demand refresh button.
--
-- The signal value is asymmetric in classical literature: clustered
-- open-market BUYS (code 'P') by officers / directors are a moderately
-- strong forward-return signal; SALES (code 'S') are noisier because
-- of 10b5-1 plans, tax-loss harvesting, and grant-driven liquidation.
-- We store both unfiltered — the UI surfaces "net buys" but the raw
-- log is available for analysis.

CREATE TABLE insider_transactions (
    transaction_id      BIGSERIAL    PRIMARY KEY,
    symbol              TEXT         NOT NULL,
    transaction_date    DATE         NOT NULL,
    filed_date          DATE,
    insider_name        TEXT,
    insider_title       TEXT,                              -- e.g. 'CEO', 'CFO', 'Director', '10% Owner'
    transaction_code    CHAR(1)      NOT NULL,             -- 'P' (purchase) | 'S' (sale)
    shares              NUMERIC(20,6),
    price               NUMERIC(20,6),
    value               NUMERIC(28,2),                     -- shares × price; signed for UI grouping
    shares_owned_after  NUMERIC(20,6),
    raw                 JSONB,                             -- provider payload for debugging
    inserted_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    -- Dedup natural key: same insider, same day, same code, same
    -- share count → same filing. EODHD occasionally republishes; we
    -- want an idempotent upsert.
    CONSTRAINT insider_transactions_natural_key UNIQUE (
        symbol, transaction_date, insider_name, transaction_code, shares
    ),
    CONSTRAINT insider_transactions_code_check CHECK (transaction_code IN ('P', 'S'))
);

CREATE INDEX idx_insider_symbol_date
    ON insider_transactions (symbol, transaction_date DESC);
CREATE INDEX idx_insider_code_date
    ON insider_transactions (transaction_code, transaction_date DESC);

-- ===========================================================
-- Refresh log — cooldown tracker so 10-credit calls don't fire
-- twice in the same day even across app restarts / page reloads.
-- ===========================================================
CREATE TABLE insider_refresh_log (
    refresh_id              BIGSERIAL    PRIMARY KEY,
    scope                   TEXT         NOT NULL,         -- 'watchlist' | 'symbol:AAPL' | etc
    started_at              TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    completed_at            TIMESTAMPTZ,
    success                 BOOLEAN      NOT NULL DEFAULT FALSE,
    symbols_refreshed       INTEGER      NOT NULL DEFAULT 0,
    transactions_upserted   INTEGER      NOT NULL DEFAULT 0,
    error_message           TEXT
);

CREATE INDEX idx_insider_refresh_log_scope
    ON insider_refresh_log (scope, completed_at DESC);
