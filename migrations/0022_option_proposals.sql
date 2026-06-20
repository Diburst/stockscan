-- 0022_option_proposals.sql
--
-- Persisted output of the options-premium proposal engine
-- (stockscan.proposals). A "run" is one invocation of the engine for a given
-- as_of date; each run holds a ranked book of short-premium proposals.
--
-- Distinct from `signals` on purpose: a directional swing signal has
-- entry/stop/qty, whereas an options proposal has side/strike/dte/credit/size —
-- a genuinely different shape (see options_proposal_engine_design.md §1, §9).
--
-- Persistence is opt-in (the `--save` flag on `stockscan options propose`); the
-- MCP tool and the /options page compute on-demand and do not require these
-- tables to exist.

CREATE TABLE option_proposal_runs (
    run_id          BIGSERIAL PRIMARY KEY,
    as_of           DATE        NOT NULL,
    list_id         BIGINT,
    regime_label    TEXT,
    composite_score NUMERIC,
    candidates      INTEGER     NOT NULL,   -- cleared filters before diversification
    book_size       INTEGER     NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE option_proposals (
    proposal_id      BIGSERIAL PRIMARY KEY,
    run_id           BIGINT  NOT NULL REFERENCES option_proposal_runs (run_id) ON DELETE CASCADE,
    rank             INTEGER NOT NULL,           -- 1 = highest score in the book
    symbol           TEXT    NOT NULL,
    side             TEXT    NOT NULL,           -- 'sell_put' | 'sell_call'
    expiry_date      DATE,
    days_to_expiry   INTEGER NOT NULL,
    strike           NUMERIC NOT NULL,
    delta            NUMERIC,
    est_credit       NUMERIC,                    -- BS fair value per share
    pct_otm          NUMERIC,
    iv_pct           NUMERIC,
    score            NUMERIC NOT NULL,
    size_weight      NUMERIC NOT NULL,
    day_move_pct     NUMERIC,
    days_to_earnings INTEGER,
    confluence_count INTEGER,
    pct_to_threat    NUMERIC,
    trend_bucket     TEXT,
    rationale        TEXT,
    price_at_level   BOOLEAN NOT NULL DEFAULT FALSE,  -- context flag (not scored)
    score_breakdown  JSONB
);

CREATE INDEX idx_option_proposals_run ON option_proposals (run_id, rank);
CREATE INDEX idx_option_proposal_runs_as_of ON option_proposal_runs (as_of DESC);
