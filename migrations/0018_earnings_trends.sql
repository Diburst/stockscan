-- 0018_earnings_trends.sql
--
-- Forward analyst estimates + revision drift (EODHD /api/calendar/trends).
--
-- This is the "analyst expectations" layer that pairs with the existing
-- earnings_calendar (which only holds report dates + actual vs estimate
-- AT release). Trends captures the WALK of consensus over time — the
-- estimate-revision dynamics that PEAD / drift-after-revisions trading
-- strategies feed on.
--
-- One row per (symbol, period_end, period) where period is the EODHD
-- horizon label: '0q' (current quarter), '+1q' (next quarter), '0y'
-- (current fiscal year), '+1y' (next fiscal year). Reset on each pull
-- via UPSERT so stale rows for closed quarters fall out naturally.

CREATE TABLE earnings_trends (
    trend_id                BIGSERIAL    PRIMARY KEY,
    symbol                  TEXT         NOT NULL,
    period_end              DATE         NOT NULL,        -- quarter/year end date the estimate refers to
    period                  TEXT         NOT NULL,        -- '0q' | '+1q' | '0y' | '+1y'

    -- EPS consensus + range + analyst coverage
    eps_estimate_avg        NUMERIC(20,6),
    eps_estimate_low        NUMERIC(20,6),
    eps_estimate_high       NUMERIC(20,6),
    eps_year_ago            NUMERIC(20,6),                -- comparable prior period EPS
    eps_growth              NUMERIC(20,6),                -- vs prior comparable period
    eps_analyst_count       INTEGER,

    -- Revenue consensus + range + analyst coverage
    rev_estimate_avg        NUMERIC(28,2),                -- typically billions; widen
    rev_estimate_low        NUMERIC(28,2),
    rev_estimate_high       NUMERIC(28,2),
    rev_year_ago            NUMERIC(28,2),
    rev_growth              NUMERIC(20,6),
    rev_analyst_count       INTEGER,

    -- EPS trend walk: where did consensus sit at these snapshots?
    eps_trend_current       NUMERIC(20,6),
    eps_trend_7d_ago        NUMERIC(20,6),
    eps_trend_30d_ago       NUMERIC(20,6),
    eps_trend_60d_ago       NUMERIC(20,6),
    eps_trend_90d_ago       NUMERIC(20,6),

    -- Revision counts — the directly-actionable signal.
    eps_revisions_up_7d     INTEGER,
    eps_revisions_up_30d    INTEGER,
    eps_revisions_down_30d  INTEGER,

    fetched_at              TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    CONSTRAINT earnings_trends_natural_key UNIQUE (symbol, period_end, period)
);

CREATE INDEX idx_earnings_trends_symbol     ON earnings_trends (symbol, period_end);
CREATE INDEX idx_earnings_trends_period_end ON earnings_trends (period_end);
