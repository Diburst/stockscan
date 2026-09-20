-- 0027_option_proposal_outcomes.sql
--
-- Options book: outcome tracking plus the filters-and-one-rank-key shape.
--
--   * option_proposals gains the settle columns the nightly job fills once a
--     proposal's expiry has passed: breached (a close through the strike),
--     touched (an intraday print through it), breach_date, close_at_expiry,
--     max_adverse_pct, settled_at. These are what turn the engine's thresholds
--     into measured base rates.
--   * The blended 0–1 score and its inputs are retired: score,
--     confluence_count, pct_to_threat, price_at_level go. In their place the
--     row records what the seller reads — hv_pct (renamed from iv_pct, the
--     label now matches the realized-vol input), move_sigma (today's move in
--     units of the name's daily vol), rank_key (move_sigma × trend alignment),
--     sigma_distance (strike distance in σ of the tenor), credit_yield_ann,
--     contracts (per-trade size against live equity), earnings_known.
--   * option_proposal_runs records the regime controls the book was built
--     under (trend gate, credit stress), the book multiplier, and the
--     high-importance macro events inside the expiry window.
--
-- The iv_pct → hv_pct rename is a plain RENAME COLUMN and therefore one-shot
-- (same as 0025's composite_score → vol_scalar); everything else is
-- idempotent. Existing rows are opt-in --save output, so they are left as
-- they are with NULLs in the new columns.

ALTER TABLE option_proposals RENAME COLUMN iv_pct TO hv_pct;

ALTER TABLE option_proposals
    DROP COLUMN IF EXISTS score,
    DROP COLUMN IF EXISTS confluence_count,
    DROP COLUMN IF EXISTS pct_to_threat,
    DROP COLUMN IF EXISTS price_at_level,
    ADD COLUMN IF NOT EXISTS contracts             INTEGER,
    ADD COLUMN IF NOT EXISTS sigma_distance        NUMERIC(8,4),
    ADD COLUMN IF NOT EXISTS credit_yield_ann      NUMERIC(8,4),
    ADD COLUMN IF NOT EXISTS earnings_known        BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS move_sigma            NUMERIC(8,4),
    ADD COLUMN IF NOT EXISTS day_move_residual_pct NUMERIC(8,4),
    ADD COLUMN IF NOT EXISTS rank_key              NUMERIC(10,6),
    ADD COLUMN IF NOT EXISTS breached              BOOLEAN,
    ADD COLUMN IF NOT EXISTS touched               BOOLEAN,
    ADD COLUMN IF NOT EXISTS breach_date           DATE,
    ADD COLUMN IF NOT EXISTS close_at_expiry       NUMERIC(14,4),
    ADD COLUMN IF NOT EXISTS max_adverse_pct       NUMERIC(8,4),
    ADD COLUMN IF NOT EXISTS settled_at            TIMESTAMPTZ;

ALTER TABLE option_proposal_runs
    ADD COLUMN IF NOT EXISTS trend_gate_open    BOOLEAN,
    ADD COLUMN IF NOT EXISTS credit_stress_flag BOOLEAN,
    ADD COLUMN IF NOT EXISTS book_mult          NUMERIC(6,4),
    ADD COLUMN IF NOT EXISTS macro_events       TEXT;

CREATE INDEX IF NOT EXISTS idx_option_proposals_unsettled
    ON option_proposals (expiry_date) WHERE settled_at IS NULL;

COMMENT ON COLUMN option_proposals.hv_pct IS
    'Annualised realized vol (EWMA Yang-Zhang) the leg was priced off, in percent.';
COMMENT ON COLUMN option_proposals.move_sigma IS
    'The day move that fired the trigger, in units of the name''s daily vol. Sector-residual for put-sales, raw for call-sales.';
COMMENT ON COLUMN option_proposals.rank_key IS
    '|move_sigma| × trend alignment. The only ranking input.';
COMMENT ON COLUMN option_proposals.sigma_distance IS
    'Strike distance from spot in σ of the tenor''s expected move.';
COMMENT ON COLUMN option_proposals.credit_yield_ann IS
    'Estimated credit as an annualised percent of the strike.';
COMMENT ON COLUMN option_proposals.contracts IS
    'floor(equity × OPTIONS_RISK_PCT × book_mult / (strike × 100 × 2 × sigma_tenor)).';
COMMENT ON COLUMN option_proposals.breached IS
    'A close crossed the strike on or before expiry. Filled by the nightly settle step.';
COMMENT ON COLUMN option_proposals.touched IS
    'An intraday high/low crossed the strike on or before expiry.';
COMMENT ON COLUMN option_proposals.max_adverse_pct IS
    'Worst close versus the strike, signed toward the short side.';
COMMENT ON COLUMN option_proposal_runs.macro_events IS
    'High-importance US events inside the expiry window, joined by a middle dot.';
