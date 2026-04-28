-- 0007_backtest_trade_context.sql
--
-- Add entry_metadata to backtest_trades so the trade log + per-trade chart
-- markers can show the indicator values that fired the entry (RSI value,
-- MACD histogram, distance below SMA200, etc.). The strategy already
-- emits these in RawSignal.metadata; we just plumb them through the
-- engine and persist alongside each trade.
--
-- Also: pre-existing rows had exit_reason = 'backtest' (placeholder bug);
-- new runs will store the actual ExitDecision.reason. Old rows aren't
-- backfilled — re-run them to populate properly.

ALTER TABLE backtest_trades
    ADD COLUMN entry_metadata JSONB;
