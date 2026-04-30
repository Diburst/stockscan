-- 0011_regime_intermediate_signals.sql
--
-- Persist the intermediate signals that feed into ``trend_score`` and
-- ``breadth_score`` so the dashboard can show "what we computed most
-- recently" per component. These columns were intentionally omitted in
-- migration 0010 (which only persisted the final scores) but the
-- dashboard's per-component breakdown wants the underlying values too.
--
-- Three columns, all NULLABLE so v1 and v2-without-these rows survive:
--
--   spy_sma200_slope_20d
--     Relative change in SPY's SMA(200) over the last 20 trading days,
--     expressed as a fraction (e.g., 0.012 = +1.2% / 20 days). Combined
--     with the close-vs-SMA200 position to drive ``trend_score``.
--
--   rsp_spy_ratio
--     Today's RSP/SPY close-price ratio. The breadth proxy compares
--     this ratio's 20-day SMA against its 200-day SMA. Stored on its
--     own so the dashboard can show the spot value.
--
--   breadth_rel_gap
--     Relative gap between the 20-day and 200-day SMAs of the RSP/SPY
--     ratio: (ratio_short_sma − ratio_long_sma) / ratio_long_sma. This
--     is the actual signal that drives ``breadth_score`` after band
--     saturation; persisting it lets the dashboard show how broad or
--     narrow the rally currently is.

ALTER TABLE market_regime
    ADD COLUMN spy_sma200_slope_20d NUMERIC(8,6),
    ADD COLUMN rsp_spy_ratio        NUMERIC(10,6),
    ADD COLUMN breadth_rel_gap      NUMERIC(8,6);

COMMENT ON COLUMN market_regime.spy_sma200_slope_20d IS
    'Relative SMA(200) change over the last 20 trading days. '
    'Feeds into trend_score via clip(slope/0.02, -1, 1).';

COMMENT ON COLUMN market_regime.rsp_spy_ratio IS
    'Spot RSP/SPY close-price ratio on as_of_date.';

COMMENT ON COLUMN market_regime.breadth_rel_gap IS
    'Relative gap between 20d and 200d SMAs of the RSP/SPY ratio. '
    'Positive = breadth broadening, negative = narrow-rally regime.';
