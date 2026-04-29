-- 0008_market_regime.sql
--
-- Persists the computed market regime for each trading day so the dashboard
-- and nightly email can display it cheaply (no re-computation on render) and
-- so backtests/historical analysis can query it later.
--
-- Regime labels (DESIGN §regime):
--   trending_up   — ADX(14) > 25 AND SPY close > SMA(200)
--   trending_down — ADX(14) > 25 AND SPY close < SMA(200)
--   choppy        — ADX(14) < 18  (range-bound, no clear direction)
--   transitioning — ADX(14) 18–25 (ambiguous; wait-and-see)

CREATE TABLE market_regime (
    as_of_date  DATE        PRIMARY KEY,
    regime      TEXT        NOT NULL
        CHECK (regime IN ('trending_up', 'trending_down', 'choppy', 'transitioning')),
    adx         NUMERIC(6,2) NOT NULL,
    spy_close   NUMERIC(10,4) NOT NULL,
    spy_sma200  NUMERIC(10,4) NOT NULL,
    computed_at TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE market_regime IS
    'Daily market-regime classification derived from SPY ADX(14) + SMA(200).';
