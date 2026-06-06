-- 0017_economic_events.sql
--
-- Economic events calendar (EODHD /api/economic-events).
--
-- Stores macro releases (CPI, NFP, FOMC, ISM, etc.) with their actual /
-- previous / estimate values, alongside an importance bucket that is
-- assigned at upsert time from the event_type. Dashboard + analysis
-- pages render the next 5-7 days filtered to high-importance US events;
-- the rest is kept for historical context and post-release surprise
-- analysis.

CREATE TABLE economic_events (
    event_id            BIGSERIAL    PRIMARY KEY,
    event_ts            TIMESTAMPTZ  NOT NULL,
    country             TEXT         NOT NULL,           -- ISO-3166 alpha-2
    event_type          TEXT         NOT NULL,           -- e.g. 'CPI', 'Nonfarm Payrolls'
    comparison          TEXT,                            -- 'mom' | 'qoq' | 'yoy' | NULL
    period              TEXT,                            -- e.g. 'Q4', 'Jan' — NULL if not applicable
    actual              NUMERIC(20,6),
    previous            NUMERIC(20,6),
    estimate            NUMERIC(20,6),
    change_value        NUMERIC(20,6),
    change_pct          NUMERIC(12,4),
    -- 'high' | 'medium' | 'low' — assigned by stockscan.econ_events.importance
    -- from event_type at upsert time. Kept on the row so the dashboard
    -- query is a plain WHERE filter, not a join + Python dispatch.
    importance          TEXT         NOT NULL DEFAULT 'low',
    fetched_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    -- Dedup: a re-pull of the same window upserts in place rather than
    -- duplicating. comparison is nullable so we coalesce to '' in the
    -- unique index — Postgres treats NULL as distinct otherwise and the
    -- dedup would silently fail for events without a comparison kind.
    CONSTRAINT econ_events_natural_key UNIQUE (
        event_ts, country, event_type
    )
);

CREATE INDEX idx_econ_events_ts ON economic_events (event_ts);
CREATE INDEX idx_econ_events_country_ts ON economic_events (country, event_ts DESC);
CREATE INDEX idx_econ_events_importance_ts
    ON economic_events (importance, event_ts) WHERE importance IN ('high', 'medium');
