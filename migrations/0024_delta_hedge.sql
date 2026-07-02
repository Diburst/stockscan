-- 0024_delta_hedge.sql
--
-- Delta-hedging feature: real-time stock hedging of a (paper) short/long option
-- position. A hedge position is a fake option the user creates on the /hedge
-- page; the hedge daemon (`stockscan hedge run`) watches its underlying over the
-- EODHD real-time websocket and buys/sells stock to keep the combined book
-- delta-neutral inside a no-transaction band.
--
-- Three tables:
--   * hedge_positions   — one row per option position being hedged, plus a cache
--                         of the current stock hedge (held_shares / avg_cost /
--                         realized_hedge_pnl) kept consistent with the ledger.
--   * hedge_adjustments — append-only ledger of every stock fill the daemon made.
--   * hedge_heartbeat   — single-row daemon liveness for the page.
--
-- Local paper only — no broker. Fills are simulated at the live spot that comes
-- over the websocket. See src/stockscan/hedge/ for the implementation.

CREATE TABLE hedge_positions (
    hedge_position_id   BIGSERIAL PRIMARY KEY,
    symbol              TEXT        NOT NULL,
    option_kind         TEXT        NOT NULL,   -- 'call' | 'put'
    option_side         TEXT        NOT NULL,   -- 'short' | 'long'
    strike              NUMERIC(14, 6) NOT NULL,
    contracts           INTEGER     NOT NULL,
    multiplier          INTEGER     NOT NULL DEFAULT 100,
    expiry              TIMESTAMPTZ NOT NULL,
    premium             NUMERIC(16, 4) NOT NULL,   -- total premium magnitude (>=0)
    iv_pct              NUMERIC(10, 4),            -- annualised sigma used, in %
    rate_pct            NUMERIC(8, 4),             -- risk-free rate used, in %
    band_policy         JSONB,                     -- HedgePolicy.to_dict()
    status              TEXT        NOT NULL DEFAULT 'active',  -- active|paused|closed

    -- Cache of the live stock hedge (source of truth is hedge_adjustments;
    -- updated in the same tx as each ledger insert).
    held_shares         INTEGER     NOT NULL DEFAULT 0,   -- signed
    avg_cost            NUMERIC(14, 6) NOT NULL DEFAULT 0,
    realized_hedge_pnl  NUMERIC(16, 4) NOT NULL DEFAULT 0,

    -- Latest mark written by the daemon.
    last_spot           NUMERIC(14, 6),
    last_delta          NUMERIC(16, 6),     -- signed option-position delta (shares)
    last_target_shares  INTEGER,
    last_hedge_spot     NUMERIC(14, 6),     -- spot at last actual trade
    last_tick_at        TIMESTAMPTZ,
    iv_refreshed_on     DATE,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    closed_at           TIMESTAMPTZ,
    close_reason        TEXT,               -- expiry_settle | manual_close
    settlement_spot     NUMERIC(14, 6),
    realized_pnl        NUMERIC(16, 4),     -- final booked P&L on close
    notes               TEXT,

    CONSTRAINT hedge_positions_kind_ck  CHECK (option_kind IN ('call', 'put')),
    CONSTRAINT hedge_positions_side_ck  CHECK (option_side IN ('short', 'long')),
    CONSTRAINT hedge_positions_status_ck CHECK (status IN ('active', 'paused', 'closed')),
    CONSTRAINT hedge_positions_contracts_ck CHECK (contracts > 0),
    CONSTRAINT hedge_positions_strike_ck CHECK (strike > 0)
);

CREATE INDEX idx_hedge_positions_status ON hedge_positions (status);
CREATE INDEX idx_hedge_positions_symbol ON hedge_positions (symbol);


CREATE TABLE hedge_adjustments (
    adjustment_id       BIGSERIAL PRIMARY KEY,
    hedge_position_id   BIGINT      NOT NULL REFERENCES hedge_positions (hedge_position_id),
    ts                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    side                TEXT        NOT NULL,   -- 'buy' | 'sell'
    qty                 INTEGER     NOT NULL,   -- positive magnitude
    price               NUMERIC(14, 6) NOT NULL,  -- fill price (= spot in paper)
    spot                NUMERIC(14, 6),
    option_delta        NUMERIC(16, 6),         -- signed position delta at the fill
    target_shares       INTEGER,
    held_before         INTEGER,
    held_after          INTEGER,
    reason              TEXT,                   -- band_breach|expiry_settle|manual_close|startup_reconcile
    realized_pnl_delta  NUMERIC(16, 4) NOT NULL DEFAULT 0,

    CONSTRAINT hedge_adjustments_side_ck CHECK (side IN ('buy', 'sell')),
    CONSTRAINT hedge_adjustments_qty_ck  CHECK (qty > 0)
);

CREATE INDEX idx_hedge_adjustments_position ON hedge_adjustments (hedge_position_id, ts);


CREATE TABLE hedge_heartbeat (
    id                  INTEGER     PRIMARY KEY DEFAULT 1,
    pid                 INTEGER,
    started_at          TIMESTAMPTZ,
    last_heartbeat_at   TIMESTAMPTZ,
    feed_kind           TEXT,
    active_symbols      INTEGER,
    status              TEXT,
    note                TEXT,

    CONSTRAINT hedge_heartbeat_singleton_ck CHECK (id = 1)
);
