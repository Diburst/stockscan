-- 0010_regime_composite.sql
--
-- Regime detection v2 (DESIGN §regime — composite score upgrade).
--
-- Two changes:
--
--   1. New table `macro_series` for daily-frequency scalar macro time series
--      (HY OAS today; room for VIX_FRED, yield-curve spreads, dollar index,
--      etc. later). Each (series_code, as_of_date) is a single point.
--      VIX itself is stored in the `bars` hypertable via the EODHD .INDX
--      exchange path so we keep its OHLC; macro_series is for series we only
--      have a level for.
--
--   2. Extend `market_regime` with the composite score plus its four
--      components (vol/trend/breadth/credit), the underlying levels we
--      pulled to compute them, a credit-stress flag, and a
--      methodology_version column so v1 rows (ADX/SMA only) and v2 rows
--      (composite) are unambiguously distinguishable for backtest replay.
--
--      All v2 score columns are NULLABLE on purpose — detect_regime() must
--      degrade gracefully when a data source (EODHD .INDX for VIX, EODHD .US
--      for RSP, FRED for HY OAS) is unreachable, persisting whatever
--      components it managed to compute. composite_score is computed by
--      renormalizing remaining weights when one or more components are NULL,
--      so it is also NULL only when every component is missing.
--
--      The legacy adx/spy_close/spy_sma200 columns and the legacy `regime`
--      text label are preserved unchanged for back-compat with v1 callers
--      and the existing dashboard.

-- ============================================================
-- 1. Macro series table (HY OAS, plus headroom for future series)
-- ============================================================
CREATE TABLE macro_series (
    series_code   TEXT          NOT NULL,                  -- e.g., 'BAMLH0A0HYM2'
    as_of_date    DATE          NOT NULL,
    value         NUMERIC(18,6) NOT NULL,
    source        TEXT          NOT NULL,                  -- 'fred', 'eodhd', etc.
    fetched_at    TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    PRIMARY KEY (series_code, as_of_date)
);

COMMENT ON TABLE macro_series IS
    'Daily-frequency scalar macro time series (HY OAS, etc.). Per-series '
    'points keyed by (series_code, as_of_date). Use the bars hypertable for '
    'OHLC instruments. macro_series is for level-only series.';

-- The PK btree on (series_code, as_of_date) already covers point lookups
-- and forward range scans within a series. Add a descending partial index
-- only if a future query pattern needs it.

-- ============================================================
-- 2. Extend market_regime with v2 composite columns
-- ============================================================
-- Component scores: each is in [0, 1] where 1.0 = "healthy / calm" axis.
-- All NULLABLE so a partial row can be persisted under degraded data.
ALTER TABLE market_regime
    ADD COLUMN composite_score  NUMERIC(6,4)
        CHECK (composite_score IS NULL OR (composite_score BETWEEN 0 AND 1)),
    ADD COLUMN vol_score        NUMERIC(6,4)
        CHECK (vol_score        IS NULL OR (vol_score        BETWEEN 0 AND 1)),
    ADD COLUMN trend_score      NUMERIC(6,4)
        CHECK (trend_score      IS NULL OR (trend_score      BETWEEN 0 AND 1)),
    ADD COLUMN breadth_score    NUMERIC(6,4)
        CHECK (breadth_score    IS NULL OR (breadth_score    BETWEEN 0 AND 1)),
    ADD COLUMN credit_score     NUMERIC(6,4)
        CHECK (credit_score     IS NULL OR (credit_score     BETWEEN 0 AND 1));

-- Underlying levels — useful for the dashboard banner and for sanity-checking
-- the derived scores. NULL when the corresponding source was unavailable.
ALTER TABLE market_regime
    ADD COLUMN vix_level        NUMERIC(7,4),
    ADD COLUMN vix_pct_rank     NUMERIC(6,4)
        CHECK (vix_pct_rank     IS NULL OR (vix_pct_rank     BETWEEN 0 AND 1)),
    ADD COLUMN hy_oas_level     NUMERIC(7,4),
    ADD COLUMN hy_oas_pct_rank  NUMERIC(6,4)
        CHECK (hy_oas_pct_rank  IS NULL OR (hy_oas_pct_rank  BETWEEN 0 AND 1)),
    ADD COLUMN hy_oas_zscore    NUMERIC(7,4);

-- Tail-risk circuit-breaker flag (research doc §Tier 0(b)):
-- HY OAS pct rank > 0.85 AND rising over trailing 5 trading days.
-- Wired in the runner as a sizing override (×0.5 + skip new longs), not
-- as part of credit_score itself (which stays the smooth 1 - rank).
ALTER TABLE market_regime
    ADD COLUMN credit_stress_flag BOOLEAN NOT NULL DEFAULT FALSE;

-- Methodology version: 1 = ADX/SMA only (pre-0010), 2 = composite (this
-- migration onwards). Backfill existing rows to 1, then change the default
-- to 2 so future inserts get the new version automatically.
ALTER TABLE market_regime
    ADD COLUMN methodology_version SMALLINT NOT NULL DEFAULT 1;
ALTER TABLE market_regime
    ALTER COLUMN methodology_version SET DEFAULT 2;

COMMENT ON COLUMN market_regime.composite_score IS
    'Weighted regime health score in [0,1] (1=healthy). Weights: vol 0.40, '
    'trend 0.25, breadth 0.20, credit 0.15. Renormalized over non-NULL '
    'components when a data source is degraded. NULL only when all four '
    'components are NULL.';
COMMENT ON COLUMN market_regime.credit_stress_flag IS
    'Tail-risk circuit breaker. TRUE when HY OAS pct rank > 0.85 AND rising '
    'over trailing 5 trading days. Used as a sizing override in the scanner '
    'runner. Orthogonal to credit_score, which stays smooth.';
COMMENT ON COLUMN market_regime.methodology_version IS
    '1 = legacy ADX(14)+SMA(200) only. 2 = composite (vol/trend/breadth/'
    'credit) added in migration 0010.';
