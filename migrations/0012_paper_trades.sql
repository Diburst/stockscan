-- Paper trading — in-app simulated positions opened from signals.
--
-- Stores the full entry snapshot (signal metadata, indicators, strategy
-- params) at open time and the corresponding exit snapshot at close time.
-- Daily mark-to-market updates unrealised P/L, MFE, and MAE from bars.

CREATE TABLE IF NOT EXISTS paper_trades (
    paper_trade_id  BIGSERIAL       PRIMARY KEY,
    signal_id       BIGINT          NOT NULL REFERENCES signals(signal_id),
    strategy_name   TEXT            NOT NULL,
    strategy_version TEXT           NOT NULL,
    symbol          TEXT            NOT NULL,
    side            TEXT            NOT NULL CHECK (side IN ('long', 'short')),

    -- Entry snapshot (captured at open time)
    entry_price     NUMERIC(14,6)   NOT NULL,
    stop_price      NUMERIC(14,6)   NOT NULL,
    target_price    NUMERIC(14,6),
    qty             INTEGER         NOT NULL CHECK (qty > 0),
    opened_at       TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

    -- Entry-time context (frozen at open)
    entry_signal_metadata   JSONB,          -- signal.metadata snapshot
    entry_tech_score        JSONB,          -- technical_scores row snapshot
    entry_regime            JSONB,          -- market_regime row snapshot
    entry_strategy_params   JSONB,          -- strategy_configs.params_json snapshot

    -- Live P/L tracking (updated by mark-to-market)
    current_price       NUMERIC(14,6),
    unrealised_pnl      NUMERIC(14,4),
    unrealised_pnl_pct  NUMERIC(8,4),
    max_favorable_excursion  NUMERIC(8,4),  -- best unrealised P/L pct
    max_adverse_excursion    NUMERIC(8,4),  -- worst unrealised P/L pct
    last_mark_at        TIMESTAMPTZ,

    -- Close (populated when trade is closed)
    status          TEXT            NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    closed_at       TIMESTAMPTZ,
    exit_price      NUMERIC(14,6),
    exit_reason     TEXT,           -- 'manual', 'stop_hit', 'target_hit', 'time_stop', strategy-specific
    realized_pnl    NUMERIC(14,4),
    realized_pnl_pct NUMERIC(8,4),
    holding_days    INTEGER,

    -- Exit-time context (frozen at close)
    exit_signal_metadata    JSONB,          -- re-computed indicators at close
    exit_tech_score         JSONB,
    exit_regime             JSONB,
    exit_strategy_params    JSONB,

    -- Auto-close configuration (copied from strategy at open time)
    auto_close_rules        JSONB,          -- {stop, target, time_stop_days, ...}

    FOREIGN KEY (strategy_name, strategy_version)
        REFERENCES strategy_versions(strategy_name, strategy_version)
);

CREATE INDEX IF NOT EXISTS idx_paper_trades_status ON paper_trades(status);
CREATE INDEX IF NOT EXISTS idx_paper_trades_symbol ON paper_trades(symbol);
CREATE INDEX IF NOT EXISTS idx_paper_trades_signal ON paper_trades(signal_id);
