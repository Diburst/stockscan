-- 0002_backtest_tables.sql
--
-- Backtest results live in their own tables — they are regenerable, often
-- deleted/recreated during research, and shouldn't pollute live trading data.
-- The schema mirrors the live trade/equity tables in shape so analytics code
-- can be reused via UNION views or polymorphic queries.

CREATE TABLE backtest_runs (
    run_id           BIGSERIAL PRIMARY KEY,
    strategy_name    TEXT          NOT NULL,
    strategy_version TEXT          NOT NULL,
    params_json      JSONB         NOT NULL,
    params_hash      TEXT          NOT NULL,
    start_date       DATE          NOT NULL,
    end_date         DATE          NOT NULL,
    starting_capital NUMERIC(16,4) NOT NULL,
    ending_equity    NUMERIC(16,4),
    num_trades       INTEGER,
    metrics_json     JSONB,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    note             TEXT,
    FOREIGN KEY (strategy_name, strategy_version)
        REFERENCES strategy_versions(strategy_name, strategy_version)
);
CREATE INDEX idx_backtest_runs_strategy
    ON backtest_runs (strategy_name, created_at DESC);

CREATE TABLE backtest_trades (
    trade_id     BIGSERIAL PRIMARY KEY,
    run_id       BIGINT NOT NULL REFERENCES backtest_runs(run_id) ON DELETE CASCADE,
    symbol       TEXT NOT NULL,
    side         TEXT NOT NULL CHECK (side IN ('long','short')),
    qty          INTEGER NOT NULL,
    entry_date   DATE NOT NULL,
    entry_price  NUMERIC(14,6) NOT NULL,
    exit_date    DATE,
    exit_price   NUMERIC(14,6),
    exit_reason  TEXT,
    commission   NUMERIC(10,4) NOT NULL DEFAULT 0,
    slippage     NUMERIC(10,4) NOT NULL DEFAULT 0,
    realized_pnl NUMERIC(14,4),
    return_pct   NUMERIC(8,4),
    holding_days INTEGER,
    mfe_pct      NUMERIC(8,4),
    mae_pct      NUMERIC(8,4)
);
CREATE INDEX idx_backtest_trades_run        ON backtest_trades (run_id);
CREATE INDEX idx_backtest_trades_run_symbol ON backtest_trades (run_id, symbol);

CREATE TABLE backtest_equity_curve (
    run_id          BIGINT NOT NULL REFERENCES backtest_runs(run_id) ON DELETE CASCADE,
    as_of_date      DATE NOT NULL,
    cash            NUMERIC(16,4) NOT NULL,
    positions_value NUMERIC(16,4) NOT NULL,
    total_equity    NUMERIC(16,4) NOT NULL,
    high_water_mark NUMERIC(16,4) NOT NULL,
    num_open        INTEGER NOT NULL,
    PRIMARY KEY (run_id, as_of_date)
);
