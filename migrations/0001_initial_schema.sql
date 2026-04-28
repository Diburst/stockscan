-- 0001_initial_schema.sql — DESIGN §8 v0.6
--
-- Sets up the full production schema:
--   - TimescaleDB extension
--   - Reference data: accounts, universe_history, corporate_actions, earnings_calendar
--   - Bars hypertable + compression policy + bars_weekly continuous aggregate
--   - Strategy plugin: strategy_versions, strategy_configs, strategy_runs
--   - Trading: signals, orders, trades, tax_lots, lot_sales, equity_history, suggestions
--   - Notes: trade_notes (with FTS), trade_note_revisions
--   - Convenience: positions VIEW

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ============================================================
-- Accounts
-- ============================================================
CREATE TABLE accounts (
    account_id        BIGSERIAL PRIMARY KEY,
    broker            TEXT NOT NULL,
    broker_account_id TEXT,
    label             TEXT,
    account_type      TEXT NOT NULL CHECK (
        account_type IN ('taxable','ira','roth','paper')
    ),
    base_currency     TEXT NOT NULL DEFAULT 'USD',
    active            BOOLEAN NOT NULL DEFAULT TRUE,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================
-- Reference data
-- ============================================================
CREATE TABLE universe_history (
    symbol      TEXT NOT NULL,
    joined_date DATE NOT NULL,
    left_date   DATE,
    PRIMARY KEY (symbol, joined_date)
);

CREATE TABLE corporate_actions (
    symbol      TEXT NOT NULL,
    action_date DATE NOT NULL,
    action_type TEXT NOT NULL CHECK (
        action_type IN ('split','cash_div','stock_div','spinoff')
    ),
    ratio       NUMERIC(20,10),
    amount      NUMERIC(20,6),
    raw_payload JSONB,
    PRIMARY KEY (symbol, action_date, action_type)
);

CREATE TABLE earnings_calendar (
    symbol      TEXT NOT NULL,
    report_date DATE NOT NULL,
    time_of_day TEXT CHECK (time_of_day IN ('bmo','amc','unknown')),
    estimate    NUMERIC(12,4),
    actual      NUMERIC(12,4),
    fetched_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (symbol, report_date)
);
CREATE INDEX idx_earnings_date ON earnings_calendar (report_date);

-- ============================================================
-- BARS — TimescaleDB hypertable
-- ============================================================
CREATE TABLE bars (
    symbol     TEXT          NOT NULL,
    bar_ts     TIMESTAMPTZ   NOT NULL,
    interval   TEXT          NOT NULL DEFAULT '1d',
    open       NUMERIC(14,6) NOT NULL,
    high       NUMERIC(14,6) NOT NULL,
    low        NUMERIC(14,6) NOT NULL,
    close      NUMERIC(14,6) NOT NULL,
    adj_close  NUMERIC(14,6) NOT NULL,
    volume     BIGINT        NOT NULL,
    source     TEXT          NOT NULL DEFAULT 'eodhd',
    fetched_at TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    PRIMARY KEY (symbol, interval, bar_ts)
);

SELECT create_hypertable('bars', 'bar_ts', chunk_time_interval => INTERVAL '1 year');

ALTER TABLE bars SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'symbol,interval',
    timescaledb.compress_orderby   = 'bar_ts DESC'
);
SELECT add_compression_policy('bars', INTERVAL '7 days');

-- Continuous aggregate: weekly bars, refreshed nightly
CREATE MATERIALIZED VIEW bars_weekly
WITH (timescaledb.continuous) AS
SELECT
    symbol,
    time_bucket('1 week', bar_ts) AS week_start,
    first(open, bar_ts)     AS open,
    max(high)               AS high,
    min(low)                AS low,
    last(close, bar_ts)     AS close,
    last(adj_close, bar_ts) AS adj_close,
    sum(volume)             AS volume
FROM bars
WHERE interval = '1d'
GROUP BY symbol, week_start;

SELECT add_continuous_aggregate_policy('bars_weekly',
    start_offset      => INTERVAL '8 weeks',
    end_offset        => INTERVAL '1 day',
    schedule_interval => INTERVAL '1 day');

-- ============================================================
-- Strategy plugin
-- ============================================================
CREATE TABLE strategy_versions (
    strategy_name      TEXT NOT NULL,
    strategy_version   TEXT NOT NULL,
    display_name       TEXT NOT NULL,
    description        TEXT,
    tags               TEXT[] NOT NULL DEFAULT '{}',
    params_json_schema JSONB NOT NULL,
    code_fingerprint   TEXT NOT NULL,
    first_seen_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (strategy_name, strategy_version)
);

CREATE TABLE strategy_configs (
    config_id         BIGSERIAL PRIMARY KEY,
    strategy_name     TEXT NOT NULL,
    strategy_version  TEXT NOT NULL,
    params_json       JSONB NOT NULL,
    params_hash       TEXT NOT NULL,
    risk_pct_override NUMERIC(5,4),
    active            BOOLEAN NOT NULL DEFAULT TRUE,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by        TEXT,
    note              TEXT,
    FOREIGN KEY (strategy_name, strategy_version)
        REFERENCES strategy_versions(strategy_name, strategy_version)
);

CREATE UNIQUE INDEX idx_active_config_per_strategy
    ON strategy_configs (strategy_name) WHERE active = TRUE;

CREATE TABLE strategy_runs (
    run_id           BIGSERIAL PRIMARY KEY,
    strategy_name    TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    config_id        BIGINT NOT NULL REFERENCES strategy_configs(config_id),
    run_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    as_of_date       DATE NOT NULL,
    universe_size    INTEGER NOT NULL,
    signals_emitted  INTEGER NOT NULL,
    rejected_count   INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (strategy_name, strategy_version)
        REFERENCES strategy_versions(strategy_name, strategy_version)
);

-- ============================================================
-- Signals
-- ============================================================
CREATE TABLE signals (
    signal_id        BIGSERIAL PRIMARY KEY,
    run_id           BIGINT REFERENCES strategy_runs(run_id),
    strategy_name    TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    config_id        BIGINT NOT NULL REFERENCES strategy_configs(config_id),
    symbol           TEXT NOT NULL,
    side             TEXT NOT NULL CHECK (side IN ('long','short')),
    score            NUMERIC(10,6),
    as_of_date       DATE NOT NULL,
    suggested_entry  NUMERIC(14,6),
    suggested_stop   NUMERIC(14,6),
    suggested_target NUMERIC(14,6),
    suggested_qty    INTEGER,
    rejected_reason  TEXT,
    metadata         JSONB,
    status           TEXT NOT NULL CHECK (
        status IN ('new','ordered','rejected','expired')
    ),
    FOREIGN KEY (strategy_name, strategy_version)
        REFERENCES strategy_versions(strategy_name, strategy_version)
);
CREATE INDEX idx_signals_status_date ON signals (status, as_of_date);

-- ============================================================
-- Orders, trades, lots, sales
-- ============================================================
CREATE TABLE orders (
    order_id        BIGSERIAL PRIMARY KEY,
    account_id      BIGINT NOT NULL REFERENCES accounts(account_id),
    signal_id       BIGINT REFERENCES signals(signal_id),
    broker_order_id TEXT,
    broker          TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL CHECK (side IN ('buy','sell')),
    qty             INTEGER NOT NULL,
    order_type      TEXT NOT NULL,
    limit_price     NUMERIC(14,6),
    stop_price      NUMERIC(14,6),
    status          TEXT NOT NULL,
    submitted_at    TIMESTAMPTZ,
    filled_at       TIMESTAMPTZ,
    avg_fill_price  NUMERIC(14,6),
    commission      NUMERIC(10,4) NOT NULL DEFAULT 0
);

CREATE TABLE trades (
    trade_id          BIGSERIAL PRIMARY KEY,
    account_id        BIGINT NOT NULL REFERENCES accounts(account_id),
    symbol            TEXT NOT NULL,
    strategy          TEXT NOT NULL,
    entry_signal_id   BIGINT REFERENCES signals(signal_id),
    opened_at         TIMESTAMPTZ NOT NULL,
    closed_at         TIMESTAMPTZ,
    status            TEXT NOT NULL CHECK (status IN ('open','closed')),
    realized_pnl      NUMERIC(14,4),
    holding_days      INTEGER,
    max_favorable_excursion NUMERIC(8,4),
    max_adverse_excursion   NUMERIC(8,4)
);
CREATE INDEX idx_trades_status ON trades (status, account_id);
CREATE INDEX idx_trades_strategy_closed ON trades (strategy, closed_at)
    WHERE status = 'closed';

CREATE TABLE tax_lots (
    lot_id          BIGSERIAL PRIMARY KEY,
    account_id      BIGINT NOT NULL REFERENCES accounts(account_id),
    trade_id        BIGINT NOT NULL REFERENCES trades(trade_id),
    symbol          TEXT NOT NULL,
    strategy        TEXT NOT NULL,
    qty_original    INTEGER NOT NULL,
    qty_remaining   INTEGER NOT NULL CHECK (qty_remaining >= 0),
    cost_basis      NUMERIC(14,6) NOT NULL,
    acquired_at     TIMESTAMPTZ NOT NULL,
    source_order_id BIGINT REFERENCES orders(order_id),
    closed_at       TIMESTAMPTZ
);
CREATE INDEX idx_lots_open ON tax_lots (account_id, symbol)
    WHERE qty_remaining > 0;
CREATE INDEX idx_lots_trade ON tax_lots (trade_id);

CREATE TABLE lot_sales (
    sale_id             BIGSERIAL PRIMARY KEY,
    sell_order_id       BIGINT NOT NULL REFERENCES orders(order_id),
    lot_id              BIGINT NOT NULL REFERENCES tax_lots(lot_id),
    qty_sold            INTEGER NOT NULL,
    sale_price          NUMERIC(14,6) NOT NULL,
    sold_at             TIMESTAMPTZ NOT NULL,
    realized_pnl        NUMERIC(14,4) NOT NULL,
    holding_period_days INTEGER NOT NULL
);

CREATE VIEW positions AS
SELECT account_id, symbol, strategy,
       SUM(qty_remaining) AS qty,
       SUM(qty_remaining * cost_basis)
           / NULLIF(SUM(qty_remaining), 0) AS avg_cost,
       MIN(acquired_at) AS first_acquired
FROM tax_lots
WHERE qty_remaining > 0
GROUP BY account_id, symbol, strategy;

-- ============================================================
-- NAV history and suggestions
-- ============================================================
CREATE TABLE equity_history (
    account_id      BIGINT NOT NULL REFERENCES accounts(account_id),
    as_of_date      DATE NOT NULL,
    cash            NUMERIC(16,4) NOT NULL,
    positions_value NUMERIC(16,4) NOT NULL,
    total_equity    NUMERIC(16,4) NOT NULL,
    high_water_mark NUMERIC(16,4) NOT NULL,
    PRIMARY KEY (account_id, as_of_date)
);

CREATE TABLE suggestions (
    suggestion_id  BIGSERIAL PRIMARY KEY,
    account_id     BIGINT NOT NULL REFERENCES accounts(account_id),
    signal_id      BIGINT NOT NULL REFERENCES signals(signal_id),
    suggested_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    action         TEXT NOT NULL,
    qty            INTEGER NOT NULL,
    user_action    TEXT NOT NULL DEFAULT 'pending'
                   CHECK (user_action IN ('taken','skipped','pending')),
    user_action_at TIMESTAMPTZ,
    journal_notes  TEXT
);

-- ============================================================
-- Trade notes (Story 6) — FTS-enabled
-- ============================================================
CREATE TABLE trade_notes (
    note_id         BIGSERIAL PRIMARY KEY,
    trade_id        BIGINT NOT NULL REFERENCES trades(trade_id) ON DELETE CASCADE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    note_type       TEXT NOT NULL CHECK (note_type IN ('entry','mid','exit','free')),
    body            TEXT NOT NULL,
    template_fields JSONB,
    body_tsv        tsvector GENERATED ALWAYS AS (to_tsvector('english', body)) STORED
);
CREATE INDEX idx_notes_trade ON trade_notes (trade_id, created_at);
CREATE INDEX idx_notes_fts   ON trade_notes USING GIN (body_tsv);

CREATE TABLE trade_note_revisions (
    revision_id            BIGSERIAL PRIMARY KEY,
    note_id                BIGINT NOT NULL REFERENCES trade_notes(note_id) ON DELETE CASCADE,
    body_before            TEXT NOT NULL,
    template_fields_before JSONB,
    edited_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
