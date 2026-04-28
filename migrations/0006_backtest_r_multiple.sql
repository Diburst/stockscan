-- 0006_backtest_r_multiple.sql
--
-- Add stop_price + r_multiple to backtest_trades so we can compute and
-- display return-on-risk per trade. R-multiple is the standard systematic-
-- trader metric:
--
--    R = (exit_price − entry_price) / (entry_price − stop_price)
--
-- A trade that hits its planned stop = −1R; a trade that gains 3× the
-- planned risk = +3R. Persisted so backtest reruns and UI rendering are
-- deterministic.
--
-- Existing rows pre-dating this migration have NULL for both columns; the
-- UI renders "—" when missing.

ALTER TABLE backtest_trades
    ADD COLUMN stop_price NUMERIC(14, 6),
    ADD COLUMN r_multiple NUMERIC(8, 4);

CREATE INDEX idx_backtest_trades_run_r
    ON backtest_trades (run_id, r_multiple DESC NULLS LAST);
