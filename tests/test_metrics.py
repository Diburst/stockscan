"""Metrics module tests."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from stockscan.metrics import (
    TradeResult,
    avg_loss_pct,
    avg_win_pct,
    cagr,
    expectancy_pct,
    max_drawdown,
    performance_report,
    profit_factor,
    sharpe_ratio,
    sortino_ratio,
    win_rate,
)


def _trade(pnl: float, ret: float = 0.05, qty: int = 100) -> TradeResult:
    """Construct a TradeResult with a target pnl by adjusting exit price."""
    entry = Decimal("100")
    # exit = entry + pnl/qty
    exit_price = entry + Decimal(str(pnl)) / Decimal(qty)
    return TradeResult(
        symbol="X",
        entry_date=date(2024, 1, 1),
        exit_date=date(2024, 1, 5),
        entry_price=entry,
        exit_price=exit_price,
        qty=qty,
    )


def test_win_rate():
    trades = [_trade(100), _trade(-50), _trade(80), _trade(-20)]
    assert win_rate(trades) == 0.5


def test_profit_factor():
    trades = [_trade(200), _trade(-50), _trade(150), _trade(-100)]
    # Gross win 350 / gross loss 150 = 2.333
    pf = profit_factor(trades)
    assert pf == 350 / 150


def test_profit_factor_no_losses_returns_inf():
    trades = [_trade(100), _trade(50)]
    assert profit_factor(trades) == float("inf")


def test_avg_win_loss():
    # +1% win, +0.5% win, -1% loss
    trades = [
        TradeResult("X", date(2024,1,1), date(2024,1,2),
                    Decimal("100"), Decimal("101"), 100),
        TradeResult("X", date(2024,1,1), date(2024,1,2),
                    Decimal("100"), Decimal("100.5"), 100),
        TradeResult("X", date(2024,1,1), date(2024,1,2),
                    Decimal("100"), Decimal("99"), 100),
    ]
    assert avg_win_pct(trades) == 0.0075  # mean of 0.01, 0.005
    assert avg_loss_pct(trades) == -0.01


def test_expectancy_pct():
    trades = [_trade(50), _trade(-25)]
    # Returns: +0.005, -0.0025 → mean ≈ +0.00125
    assert expectancy_pct(trades) == (0.005 + -0.0025) / 2


def test_cagr_doubles_in_one_year():
    idx = pd.date_range("2024-01-01", "2025-01-01", freq="D")
    equity = pd.Series(np.linspace(1, 2, len(idx)), index=idx)
    c = cagr(equity)
    assert 0.95 < c < 1.05  # ~100% growth


def test_max_drawdown_simple():
    # 100 → 110 → 80 → 90 → 105 → 130
    idx = pd.date_range("2024-01-01", periods=6, freq="D")
    equity = pd.Series([100, 110, 80, 90, 105, 130], index=idx)
    dd, days = max_drawdown(equity)
    # Peak 110, trough 80 → −27.27%
    assert dd == -30 / 110
    assert days >= 1


def test_max_drawdown_zero_for_monotonic():
    idx = pd.date_range("2024-01-01", periods=10, freq="D")
    equity = pd.Series(np.linspace(100, 200, 10), index=idx)
    dd, days = max_drawdown(equity)
    assert dd == 0
    assert days == 0


def test_sharpe_zero_for_constant_equity():
    idx = pd.date_range("2024-01-01", periods=100, freq="D")
    equity = pd.Series([100.0] * 100, index=idx)
    assert sharpe_ratio(equity) == 0


def test_sortino_zero_when_no_downside():
    idx = pd.date_range("2024-01-01", periods=100, freq="D")
    equity = pd.Series(np.linspace(100, 110, 100), index=idx)
    # Strict uptrend → no downside → sortino = 0 by our convention.
    assert sortino_ratio(equity) == 0


def test_r_multiple_when_no_stop_returns_none():
    t = TradeResult("X", date(2024,1,1), date(2024,1,5),
                    Decimal("100"), Decimal("110"), 100)
    assert t.r_multiple is None


def test_r_multiple_one_to_one_at_stop():
    """Exit equals stop → R = -1.0 (clean stop hit)."""
    t = TradeResult(
        "X", date(2024,1,1), date(2024,1,5),
        entry_price=Decimal("100"), exit_price=Decimal("90"),
        qty=100, entry_stop=Decimal("90"),
    )
    assert t.r_multiple == pytest.approx(-1.0)


def test_r_multiple_three_x_winner():
    """Stop $5 below entry; exit $15 above entry → R = +3.0."""
    t = TradeResult(
        "X", date(2024,1,1), date(2024,1,30),
        entry_price=Decimal("100"), exit_price=Decimal("115"),
        qty=100, entry_stop=Decimal("95"),
    )
    assert t.r_multiple == pytest.approx(3.0)


def test_r_multiple_invalid_stop_returns_none():
    """Stop ≥ entry is a strategy bug; we report None rather than infinity."""
    t = TradeResult(
        "X", date(2024,1,1), date(2024,1,5),
        entry_price=Decimal("100"), exit_price=Decimal("110"),
        qty=100, entry_stop=Decimal("100"),  # zero risk
    )
    assert t.r_multiple is None


def test_performance_report_assembles_all_fields():
    idx = pd.date_range("2024-01-01", periods=10, freq="D")
    equity = pd.Series(np.linspace(1_000_000, 1_100_000, 10), index=idx)
    pos = pd.Series([0.0] * 10, index=idx)
    report = performance_report([_trade(1000), _trade(-500)], equity, pos)
    assert report.num_trades == 2
    assert report.total_return_pct > 0
    d = report.to_dict()
    assert "sharpe" in d and "max_drawdown_pct" in d and "num_trades" in d
