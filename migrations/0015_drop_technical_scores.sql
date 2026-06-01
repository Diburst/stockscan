-- 0015_drop_technical_scores.sql
--
-- Retire the parallel per-(symbol, date, strategy) technical-score annotation.
--
-- Scoring is now owned by the strategy: a strategy computes its own
-- RawSignal.score (composing indicator primitives via a plain composite function
-- when it wants to, e.g. reversal_swing → stockscan.composites.reversal_composite),
-- and the per-input breakdown rides along in signals.metadata->'score_breakdown'.
-- The runner no longer stamps a separate "technical score" on every signal
-- (that annotation was noise on non-reversal strategies and a double-compute on
-- reversal_swing). Web routes and the CLI now read signals.score + metadata
-- instead of this table.
--
-- No other schema change: signals.score and signals.metadata already exist.

DROP INDEX IF EXISTS idx_tech_scores_symbol_date;
DROP TABLE IF EXISTS technical_scores;
