"""Base-rate analyzer — works with bars passed directly (no DB required)."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from stockscan.analyzer import compute_base_rates
from stockscan.strategies.rsi2_meanrev import RSI2MeanReversion


@pytest.fixture(autouse=True)
def _sector_legs(monkeypatch):
    """Flat sector, stock down 5% on the month — every pullback is idiosyncratic."""
    monkeypatch.setattr(
        "stockscan.strategies.rsi2_meanrev.sector_return",
        lambda bars, as_of, *, lookback: 0.0,
    )
    monkeypatch.setattr(
        "stockscan.strategies.rsi2_meanrev.sector_relative_return",
        lambda bars, as_of, *, lookback: -0.05,
    )


def _build_bars_with_pullbacks(n_pullbacks: int = 3) -> pd.DataFrame:
    """Long uptrend interrupted by sharp pullbacks — same shape as backtest fixture."""
    base = []
    cur = 100.0
    for _ in range(n_pullbacks + 1):
        base.extend(np.linspace(cur, cur * 1.10, 50).tolist())
        cur = base[-1]
        base.extend(np.linspace(cur, cur * 0.95, 4).tolist())
        cur = base[-1]
        base.extend(np.linspace(cur, cur * 1.02, 5).tolist())
        cur = base[-1]
    pad = np.linspace(80, 100, 260).tolist()
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
        },
        index=idx,
    )


def test_base_rates_finds_setups():
    bars = _build_bars_with_pullbacks(n_pullbacks=3)
    report = compute_base_rates(
        RSI2MeanReversion,
        symbol="TEST",
        as_of=bars.index[-1].date(),
        bars=bars,
    )
    assert report.strategy_name == "rsi2_meanrev"
    assert report.strategy_version == RSI2MeanReversion.version
    assert report.n_setups >= 1
    assert -1 < report.expectancy_pct < 1
    assert 0 <= report.win_rate <= 1
    assert report.avg_holding_days > 0
    # Distribution length matches setup count
    assert len(report.return_distribution) == report.n_setups
    assert report.sample_size_warning is True  # < 50 setups


def test_base_rates_empty_bars_returns_warning():
    bars = pd.DataFrame()
    report = compute_base_rates(
        RSI2MeanReversion,
        symbol="ABSENT",
        as_of=date(2024, 1, 1),
        bars=bars,
    )
    assert report.n_setups == 0
    assert report.sample_size_warning is True
    assert "No bars" in report.note


def test_base_rates_never_fired_returns_warning():
    closes = np.linspace(100.0, 200.0, 400).tolist()  # RSI(2) never oversold
    idx = pd.date_range("2023-01-02", periods=len(closes), freq="B", tz="UTC")
    bars = pd.DataFrame(
        {
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "adj_close": closes,
            "volume": [1_000_000] * len(closes),
        },
        index=idx,
    )
    report = compute_base_rates(RSI2MeanReversion, "TEST", bars.index[-1].date(), bars=bars)
    assert report.n_setups == 0
    assert "never fired" in report.note


def test_base_rates_to_dict_round_trips():
    bars = _build_bars_with_pullbacks(2)
    report = compute_base_rates(RSI2MeanReversion, "TEST", bars.index[-1].date(), bars=bars)
    d = report.to_dict()
    assert d["strategy_name"] == "rsi2_meanrev"
    assert d["symbol"] == "TEST"
    assert "win_rate" in d and "expectancy_pct" in d
    assert d["n_setups"] == report.n_setups
