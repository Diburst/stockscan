-- 0014_fundamentals_widen_ratios.sql
--
-- Fix: NumericValueOutOfRange on fundamentals upsert.
--
-- Six ratio / margin columns were defined NUMERIC(8,6) — i.e. |value| must be
-- < 100 — but the underlying quantities legitimately exceed that:
--
--   * payout_ratio    — e.g. SLG = 153.75 (a REIT can pay out > 100% of EPS;
--                       EODHD also delivers this as a percentage-scale number).
--   * profit_margin / operating_margin / return_on_equity / return_on_assets
--                     — can blow well past ±100 (as a multiple) for companies
--                       with tiny or negative revenue / equity.
--   * dividend_yield  — normally small, widened for symmetry + glitch safety.
--
-- Because a single out-of-range field aborted the entire INSERT, the symbol got
-- NO fundamentals row at all (no sector, no market cap), which silently also
-- dropped it from the sector composites / sector_rs. Widening to NUMERIC(12,6)
-- gives ±999,999.999999 of headroom at the same 6-dp scale.
--
-- Belt-and-suspenders: the application layer (fundamentals.store._fit_numeric)
-- additionally coerces anything STILL out of range, or non-finite (NaN/±inf),
-- to NULL rather than raising — so a future surprise value can never again
-- abort a row.

ALTER TABLE fundamentals_snapshot
    ALTER COLUMN profit_margin    TYPE NUMERIC(12, 6),
    ALTER COLUMN operating_margin TYPE NUMERIC(12, 6),
    ALTER COLUMN return_on_equity TYPE NUMERIC(12, 6),
    ALTER COLUMN return_on_assets TYPE NUMERIC(12, 6),
    ALTER COLUMN dividend_yield   TYPE NUMERIC(12, 6),
    ALTER COLUMN payout_ratio     TYPE NUMERIC(12, 6);
