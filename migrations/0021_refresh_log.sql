-- 0021_refresh_log.sql
--
-- Generic per-scope refresh cooldown.
--
-- Slow-changing daily datasets (the economic-events calendar, the earnings
-- calendar + estimate trends) don't need to be re-fetched on every "Refresh
-- bars" click — doing so just burns EODHD credits. This table records the last
-- successful refresh per named scope so a generic cooldown gate can make
-- repeat refreshes within the window a no-op, the same way insider refreshes
-- are already gated by ``insider_refresh_log``.
--
-- One row per scope (e.g. 'econ_events', 'earnings'); upserted on success.

CREATE TABLE refresh_log (
    scope         TEXT PRIMARY KEY,
    last_success  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
