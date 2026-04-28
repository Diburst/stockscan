-- 0003_watchlist.sql
--
-- User watchlist: manually-added symbols with optional price-target alerts.
-- One row per (symbol). Alerts fire once when target is crossed, then
-- alert_enabled is set to FALSE to prevent re-alerting until the user
-- manually re-enables (matches retail watchlist semantics).

CREATE TABLE watchlist_items (
    watchlist_id           BIGSERIAL PRIMARY KEY,
    symbol                 TEXT NOT NULL UNIQUE,
    target_price           NUMERIC(14, 6),
    target_direction       TEXT CHECK (target_direction IN ('above', 'below')),
    alert_enabled          BOOLEAN NOT NULL DEFAULT TRUE,
    last_alerted_at        TIMESTAMPTZ,
    last_triggered_price   NUMERIC(14, 6),
    note                   TEXT,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- Both target columns must be set or both must be NULL.
    CONSTRAINT target_pair_consistent
        CHECK ((target_price IS NULL) = (target_direction IS NULL))
);

CREATE INDEX idx_watchlist_alert_enabled
    ON watchlist_items (alert_enabled)
    WHERE alert_enabled = TRUE AND target_price IS NOT NULL;
