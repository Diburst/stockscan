-- Prevent duplicate signals for the same (symbol, strategy, version, date).
--
-- Root cause: _persist_signal does a plain INSERT with no dedup check,
-- so every "Refresh" re-inserts all signals for the same date. This
-- migration adds a unique index so the upsert (ON CONFLICT DO UPDATE)
-- can work, and deduplicates any existing rows first.
--
-- Strategy: keep the LATEST signal_id (highest run_id) for each
-- (symbol, strategy_name, strategy_version, as_of_date) pair, delete
-- the rest. The run_id FK on the kept row points to the most recent
-- scan run, which has the freshest data.
--
-- FK handling: four tables FK to signals.signal_id —
--   paper_trades.signal_id    (NOT NULL, added in 0012)
--   orders.signal_id          (nullable, 0001)
--   trades.entry_signal_id    (nullable, 0001)
--   suggestions.signal_id     (NOT NULL, 0001)
-- Re-point any row that currently points to a "loser" duplicate over to
-- the canonical winner before deleting the loser, so the foreign-key
-- constraints don't block the dedup. Original 0013 missed this and
-- broke for any DB that had paper_trades referencing a duplicate row.
--
-- Statement design notes: the migration runner (db_migrate.apply_pending)
-- splits this file by ';' and runs each statement under AUTOCOMMIT —
-- so we cannot use TEMP TABLE / ON COMMIT DROP across statements.
-- Each UPDATE below computes the member→winner mapping inline via a
-- window function. The "AND member_id != winner_id" filter makes every
-- UPDATE idempotent, so partial failure + retry is safe.

-- Step 1: Re-point each FK table from a "loser" duplicate to the winner.

UPDATE paper_trades pt
   SET signal_id = w.winner_id
  FROM (
      SELECT signal_id AS member_id,
             MAX(signal_id) OVER (
                 PARTITION BY symbol, strategy_name, strategy_version, as_of_date
             ) AS winner_id
        FROM signals
  ) w
 WHERE pt.signal_id = w.member_id
   AND w.member_id != w.winner_id;

UPDATE orders o
   SET signal_id = w.winner_id
  FROM (
      SELECT signal_id AS member_id,
             MAX(signal_id) OVER (
                 PARTITION BY symbol, strategy_name, strategy_version, as_of_date
             ) AS winner_id
        FROM signals
  ) w
 WHERE o.signal_id = w.member_id
   AND w.member_id != w.winner_id;

UPDATE trades t
   SET entry_signal_id = w.winner_id
  FROM (
      SELECT signal_id AS member_id,
             MAX(signal_id) OVER (
                 PARTITION BY symbol, strategy_name, strategy_version, as_of_date
             ) AS winner_id
        FROM signals
  ) w
 WHERE t.entry_signal_id = w.member_id
   AND w.member_id != w.winner_id;

UPDATE suggestions s
   SET signal_id = w.winner_id
  FROM (
      SELECT signal_id AS member_id,
             MAX(signal_id) OVER (
                 PARTITION BY symbol, strategy_name, strategy_version, as_of_date
             ) AS winner_id
        FROM signals
  ) w
 WHERE s.signal_id = w.member_id
   AND w.member_id != w.winner_id;

-- Step 2: Delete duplicates, keeping the row with the highest signal_id
-- (latest insertion = most recent scan run). All FK references have been
-- repointed in step 1, so no constraint violations now.
DELETE FROM signals
 WHERE signal_id NOT IN (
     SELECT MAX(signal_id)
       FROM signals
      GROUP BY symbol, strategy_name, strategy_version, as_of_date
 );

-- Step 3: Create a unique index on the natural key.
-- This supports ON CONFLICT in the runner's INSERT.
CREATE UNIQUE INDEX IF NOT EXISTS idx_signals_natural_key
    ON signals (symbol, strategy_name, strategy_version, as_of_date);
