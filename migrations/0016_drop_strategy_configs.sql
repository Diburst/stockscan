-- 0016_drop_strategy_configs.sql
--
-- Retire the strategy_configs table. Strategy tunable knobs now live in the
-- strategy file itself — ClassVar constants for the "edit-and-bump" model
-- (ReversalSwing as of v1.1.0), pydantic Field defaults for strategies that
-- keep a params_model (rsi2_meanrev, donchian_trend, largecap_rebound,
-- momentum_52w_high). The live runner always uses the file values; nothing
-- in the DB shadows them anymore.
--
-- Past signals are still queryable via (strategy_name, strategy_version) and
-- their on-row metadata. A version bump is the unit of change for a knob
-- adjustment going forward — the strategy_versions row captures the new
-- code_fingerprint at that point.

ALTER TABLE signals       DROP COLUMN IF EXISTS config_id;
ALTER TABLE strategy_runs DROP COLUMN IF EXISTS config_id;
DROP TABLE IF EXISTS strategy_configs;
