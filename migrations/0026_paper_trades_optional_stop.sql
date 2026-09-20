-- 0026_paper_trades_optional_stop.sql
--
-- Paper trades: a stop is optional. Strategies that size by fixed fraction
-- and exit on rules alone (rsi2_meanrev) emit signals with no price stop,
-- so a paper trade opened from one carries stop_price = NULL and the
-- auto-close check skips the stop test.
--
-- Retired with this migration: entry_tech_score / exit_tech_score, the
-- reversal_swing score snapshots; that strategy is gone and the signal
-- metadata snapshots already carry every input the remaining strategies
-- produce.

ALTER TABLE paper_trades ALTER COLUMN stop_price DROP NOT NULL;
ALTER TABLE paper_trades DROP COLUMN entry_tech_score;
ALTER TABLE paper_trades DROP COLUMN exit_tech_score;
