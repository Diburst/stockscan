"""Correctness guards for the backtest performance optimizations.

These don't measure speed — they prove the fast paths produce identical results
to the slow ones they replaced:

  1. engine._bars searchsorted slice == the old `index.date <= as_of` mask.
  2. `_wilder_smoothing` on a NumPy array == the textbook pandas recursion.
  3. relative_strength caches the sector map + composite closes (one fetch
     each per run) and its searchsorted slice matches a date mask.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

import stockscan.indicators.relative_strength as srs
from stockscan.backtest.engine import BacktestConfig, BacktestEngine
from stockscan.indicators.ta import _wilder_smoothing
from stockscan.strategies.rsi2_meanrev import RSI2MeanReversion


def _frame(n: int, start: str = "2022-01-03") -> pd.DataFrame:
    idx = pd.date_range(start, periods=n, freq="B", tz="UTC")
    closes = list(80 + np.cumsum(np.random.default_rng(3).normal(0, 0.4, n)))
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c + 1 for c in closes],
            "low": [c - 1 for c in closes],
            "close": closes,
            "adj_close": closes,
            "volume": [1_000_000] * n,
        },
        index=idx,
    )


# ======================================================================
# 1. _bars slice equivalence
# ======================================================================
def test_bars_searchsorted_matches_date_mask():
    frame = _frame(400)

    def loader(symbol, start, end):
        return frame

    cfg = BacktestConfig(
        strategy_cls=RSI2MeanReversion,
        start_date=date(2022, 6, 1),
        end_date=date(2023, 6, 1),
        universe=["X"],
    )
    eng = BacktestEngine(cfg, bars_loader=loader)
    for as_of in (date(2022, 6, 15), date(2022, 12, 30), date(2023, 5, 31)):
        got = eng._bars("X", as_of)
        expected = frame[frame.index.date <= as_of]
        assert got.index.equals(expected.index)
        assert (got.index[-1].date() <= as_of) if len(got) else True


# ======================================================================
# 2. Wilder smoothing: ndarray loop == reference pandas recursion
# ======================================================================
def _reference_wilder(series: pd.Series, period: int) -> pd.Series:
    out = pd.Series(np.nan, index=series.index, dtype=float)
    if len(series) < period:
        return out
    out.iloc[period - 1] = series.iloc[:period].mean()
    for i in range(period, len(series)):
        out.iloc[i] = out.iloc[i - 1] + (series.iloc[i] - out.iloc[i - 1]) / period
    return out


@pytest.mark.parametrize("period", [2, 14])
def test_wilder_smoothing_matches_reference(period):
    s = _frame(300)["close"].diff().clip(lower=0.0)
    fast = _wilder_smoothing(s, period)
    slow = _reference_wilder(s, period)
    assert fast.index.equals(s.index)
    assert fast.isna().sum() == period - 1
    pd.testing.assert_series_equal(fast, slow, check_names=False)


def test_wilder_smoothing_short_series_is_all_nan():
    s = pd.Series([1.0, 2.0, 3.0])
    out = _wilder_smoothing(s, 5)
    assert out.isna().all() and len(out) == 3


# ======================================================================
# 3. sector_rs run-scoped caching + slice equivalence
# ======================================================================
def test_sector_rs_caches_map_and_composite(monkeypatch):
    srs.clear_cache()
    calls = {"map": 0, "bars": 0}

    comp_idx = pd.date_range("2021-01-04", periods=300, freq="B", tz="UTC")
    comp_df = pd.DataFrame({"close": np.linspace(100, 130, 300)}, index=comp_idx)

    def fake_sector_map(**_):
        calls["map"] += 1
        return {"AAPL": "Technology"}

    def fake_get_bars(symbol, start=None, end=None, **_):
        calls["bars"] += 1
        return comp_df

    monkeypatch.setattr("stockscan.sectors.store.sector_map", fake_sector_map)
    monkeypatch.setattr("stockscan.data.store.get_bars", fake_get_bars)

    # Many (symbol, day) lookups — the inner-loop pattern.
    for d in pd.date_range("2022-01-03", periods=50, freq="B"):
        comp = srs._composite_symbol_for("AAPL")
        assert comp == "$EWSECTOR:TECHNOLOGY"
        out = srs._composite_closes(comp, d.date())
        assert out is not None and not out.empty
        # searchsorted slice == date mask on the tz-naive normalised index
        expected = comp_df["close"][comp_df.index.date <= d.date()]
        assert len(out) == len(expected)
        assert out.index[-1].date() == expected.index[-1].date()

    assert calls["map"] == 1, "sector map should be fetched once per run"
    assert calls["bars"] == 1, "each composite should be fetched once per run"

    # clear_cache forces a refetch (used per backtest run / after composite rebuild).
    srs.clear_cache()
    srs._composite_symbol_for("AAPL")
    assert calls["map"] == 2
    srs.clear_cache()  # leave clean for other tests
