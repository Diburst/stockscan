-- 0025_regime_v3.sql
--
-- Market regime v3: two separate controls instead of a blended score.
--
--   * trend gate  — SPY vs SMA(200) with a 3-close dwell; governs new entries
--   * vol scalar  — realized SPY vol, percentile-ranked; shrinks size in the
--                   top tercile for strategies that opt in
--   * credit-stress flag — HY OAS breaker, unchanged
--
-- Retired with this migration: the ADX/SMA label and its four-way CHECK,
-- the 40/25/20/15 composite and its component scores, the VIX and RSP/SPY
-- inputs, the HY OAS z-score. Existing rows are dropped rather than
-- migrated — they are a cache, recomputed on demand by detect_regime().
--
-- option_proposal_runs recorded the composite for the options book; it now
-- records the vol scalar the book was sized with.

DELETE FROM market_regime;

ALTER TABLE market_regime
    DROP COLUMN IF EXISTS adx,
    DROP COLUMN IF EXISTS composite_score,
    DROP COLUMN IF EXISTS vol_score,
    DROP COLUMN IF EXISTS trend_score,
    DROP COLUMN IF EXISTS breadth_score,
    DROP COLUMN IF EXISTS credit_score,
    DROP COLUMN IF EXISTS vix_level,
    DROP COLUMN IF EXISTS vix_pct_rank,
    DROP COLUMN IF EXISTS hy_oas_zscore,
    DROP COLUMN IF EXISTS rsp_spy_ratio,
    DROP COLUMN IF EXISTS breadth_rel_gap,
    ADD COLUMN IF NOT EXISTS trend_gate_open       BOOLEAN      NOT NULL,
    ADD COLUMN IF NOT EXISTS days_on_side          INTEGER      NOT NULL,
    ADD COLUMN IF NOT EXISTS realized_vol_20d      NUMERIC(8,6),
    ADD COLUMN IF NOT EXISTS realized_vol_pct_rank NUMERIC(6,4),
    ADD COLUMN IF NOT EXISTS vol_scalar            NUMERIC(6,4),
    DROP CONSTRAINT IF EXISTS market_regime_regime_check,
    ADD CONSTRAINT market_regime_regime_check
        CHECK (regime IN ('risk_on', 'risk_off', 'credit_stress'));

ALTER TABLE market_regime ALTER COLUMN methodology_version SET DEFAULT 3;

COMMENT ON TABLE market_regime IS
    'Daily market-health controls: SPY 200-day trend gate (with dwell), '
    'realized-vol position scalar, HY OAS credit-stress breaker.';
COMMENT ON COLUMN market_regime.regime IS
    'Display label derived from the flags: risk_on / risk_off / credit_stress.';
COMMENT ON COLUMN market_regime.trend_gate_open IS
    'TRUE after 3 consecutive SPY closes above SMA(200), FALSE after 3 below. '
    'Closed gate blocks new long entries only.';
COMMENT ON COLUMN market_regime.days_on_side IS
    'Consecutive closes on the current side of SMA(200) — the dwell counter.';
COMMENT ON COLUMN market_regime.realized_vol_20d IS
    'Annualized 20-day realized volatility of SPY daily log returns.';
COMMENT ON COLUMN market_regime.realized_vol_pct_rank IS
    'Trailing 252-day percentile rank of realized_vol_20d.';
COMMENT ON COLUMN market_regime.vol_scalar IS
    'Size multiplier in [0.5, 1]: clip(0.16 / realized_vol) when the vol rank '
    'is in the top tercile, else 1. Applied only by strategies that opt in.';

ALTER TABLE option_proposal_runs RENAME COLUMN composite_score TO vol_scalar;
