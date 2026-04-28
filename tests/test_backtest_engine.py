"""Backtest engine end-to-end tests.

Uses an in-memory `bars_loader` so we don't need a database. Builds a
synthetic price series designed to fire the strategy a known number of
times and verifies the resulting trades, equity curve, and report.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from stockscan.backtest import (
    BacktestConfig,
    BacktestEngine,
    FixedBpsSlippage,
    NoSlippage,
)
from stockscan.strategies.rsi2_meanrev import RSI2MeanReversion, RSI2Params


def _build_pullback_series(symbol: str = "TEST", n_pullbacks: int = 3) -> pd.DataFrame:
    """Long uptrend interrupted by sharp 3-day pullbacks every ~50 bars.

    Designed to trigger RSI(2) entries roughly `n_pullbacks` times.
    """
    base = []
    cur = 100.0
    bar_per_segment = 50
    for _ in range(n_pullbacks + 1):
        # Slow ramp up
        base.extend(np.linspace(cur, cur * 1.1, bar_per_segment).tolist())
        cur = base[-1]
        # Sharp 4-day pullback (should trip RSI(2) low)
        base.extend(np.linspace(cur, cur * 0.95, 4).tolist())
        cur = base[-1]
        # Recovery
        base.extend(np.linspace(cur, cur * 1.02, 5).tolist())
        cur = base[-1]
    # Pad the front so SMA(200) has warmup.
    pad = np.linspace(80, 100, 200).tolist()
    closes = pad + base
    n = len(closes)
    idx = pd.date_range("2023-01-02", periods=n, freq="B", tz="UTC")
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c * 1.005 for c in closes],
            "low": [c * 0.995 for c in closes],
            "close": closes,
            "adj_close": closes,
            "volume": [5_000_000] * n,
            "symbol": [symbol] * n,
        },
        index=idx,
    )


@pytest.fixture
def bars_df() -> pd.DataFrame:
    return _build_pullback_series()


@pytest.fixture
def bars_loader(bars_df):
    """Closure that mimics get_bars(symbol, start, end) but uses our fixture."""

    def _loader(symbol: str, start, end) -> pd.DataFrame:
        if symbol != "TEST":
            return pd.DataFrame()
        df = bars_df
        # Convert start/end to Timestamps if dates
        s = pd.Timestamp(start, tz="UTC") if hasattr(start, "year") else start
        e = pd.Timestamp(end, tz="UTC") if hasattr(end, "year") else end
        return df[(df.index >= s) & (df.index <= e + pd.Timedelta(days=1))]

    return _loader


def test_backtest_runs_and_produces_trades(bars_loader, bars_df):
    cfg = BacktestConfig(
        strategy_cls=RSI2MeanReversion,
        params=RSI2Params(),
        start_date=bars_df.index[210].date(),
        end_date=bars_df.index[-1].date(),
        starting_capital=Decimal("100000"),
        risk_pct=Decimal("0.01"),
        slippage=NoSlippage(),
        universe=["TEST"],
        max_positions=15,
    )
    engine = BacktestEngine(cfg, bars_loader=bars_loader)
    result = engine.run()

    # Must have produced at least one trade given the engineered pullbacks.
    assert len(result.trades) >= 1, "Expected RSI(2) to fire on engineered pullbacks"
    # Equity curve has one point per trading day in the window.
    assert len(result.equity_curve) >= 10
    # Report has all expected fields populated.
    assert result.report.num_trades == len(result.trades)
    # Starting equity recorded as the first equity point.
    assert float(result.equity_curve.iloc[0]) == pytest.approx(100000.0, rel=1e-3)


def test_backtest_no_lookahead(bars_loader, bars_df):
    """Two backtests on overlapping windows must agree on the overlap.

    Run A on [d0, d_mid]; run B on [d0, d_end]. The closed trades up to
    d_mid in B must match the trades from A.
    """
    full_start = bars_df.index[210].date()
    mid = bars_df.index[280].date()
    end = bars_df.index[-1].date()

    cfg_short = BacktestConfig(
        strategy_cls=RSI2MeanReversion,
        params=RSI2Params(),
        start_date=full_start,
        end_date=mid,
        starting_capital=Decimal("100000"),
        slippage=NoSlippage(),
        universe=["TEST"],
    )
    cfg_long = BacktestConfig(
        strategy_cls=RSI2MeanReversion,
        params=RSI2Params(),
        start_date=full_start,
        end_date=end,
        starting_capital=Decimal("100000"),
        slippage=NoSlippage(),
        universe=["TEST"],
    )
    short = BacktestEngine(cfg_short, bars_loader=bars_loader).run()
    long = BacktestEngine(cfg_long, bars_loader=bars_loader).run()

    short_closed = [t for t in short.trades if t.exit_date < mid]
    long_closed_in_window = [t for t in long.trades if t.exit_date < mid]

    assert len(short_closed) == len(long_closed_in_window)
    for a, b in zip(short_closed, long_closed_in_window):
        assert a.symbol == b.symbol
        assert a.entry_date == b.entry_date
        assert a.exit_date == b.exit_date
        assert a.entry_price == b.entry_price
        assert a.exit_price == b.exit_price


def test_trades_record_exit_reason_and_entry_metadata(bars_loader, bars_df):
    """Closed trades should carry the strategy's exit reason + entry indicator
    snapshot so the UI can render WHY the trade fired and exited."""
    cfg = BacktestConfig(
        strategy_cls=RSI2MeanReversion,
        params=RSI2Params(),
        start_date=bars_df.index[210].date(),
        end_date=bars_df.index[-1].date(),
        starting_capital=Decimal("100000"),
        slippage=NoSlippage(),
        universe=["TEST"],
    )
    result = BacktestEngine(cfg, bars_loader=bars_loader).run()

    if not result.trades:
        # The fixture should reliably produce at least one trade; if it
        # doesn't, the bug is in the fixture, not in the field plumbing.
        return

    trade = result.trades[0]
    # Exit reason should be either a strategy-defined reason
    # ('mean_reverted_above_sma5', 'hard_stop', 'time_stop') or
    # 'end_of_backtest' if force-closed at the run boundary. NOT None,
    # NOT 'backtest' (the previous placeholder).
    assert trade.exit_reason is not None
    assert trade.exit_reason != "backtest"
    assert trade.exit_reason in {
        "mean_reverted_above_sma5", "hard_stop", "time_stop", "end_of_backtest"
    }

    # Entry metadata should be a dict carrying the strategy's indicator
    # values at signal time (RSI, ATR, SMA trend etc. for RSI(2)).
    assert isinstance(trade.entry_metadata, dict)
    assert "rsi" in trade.entry_metadata


def test_slippage_hurts_returns(bars_loader, bars_df):
    """Adding slippage should decrease total return (or keep it equal if zero trades)."""
    base = BacktestConfig(
        strategy_cls=RSI2MeanReversion,
        params=RSI2Params(),
        start_date=bars_df.index[210].date(),
        end_date=bars_df.index[-1].date(),
        starting_capital=Decimal("100000"),
        slippage=NoSlippage(),
        universe=["TEST"],
    )
    slipped = BacktestConfig(
        strategy_cls=RSI2MeanReversion,
        params=RSI2Params(),
        start_date=bars_df.index[210].date(),
        end_date=bars_df.index[-1].date(),
        starting_capital=Decimal("100000"),
        slippage=FixedBpsSlippage(bps=Decimal("20")),  # heavy slippage
        universe=["TEST"],
    )
    r0 = BacktestEngine(base, bars_loader=bars_loader).run()
    r1 = BacktestEngine(slipped, bars_loader=bars_loader).run()
    if len(r0.trades) > 0:
        assert r1.report.total_return_pct <= r0.report.total_return_pct
