"""Correctness guards for the backtest performance optimizations.

These don't measure speed — they prove the fast paths produce identical results
to the slow ones they replaced:

  1. engine._bars searchsorted slice == the old `index.date <= as_of` mask.
  2. ReversalSwing.reversal_score on a tail window == on the full history
     (the basis for reversal_swing bounding its indicator compute).
  3. relative_strength caches the sector map + composite bars (one fetch each per run).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import numpy as np
import pandas as pd

import stockscan.indicators.relative_strength as srs
from stockscan.backtest.engine import BacktestConfig, BacktestEngine
from stockscan.strategies.reversal_swing import ReversalSwing


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
            "symbol": ["X"] * n,
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
        strategy_cls=ReversalSwing,
        params=None,
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
# 2. tail invariance of the composite score
# ======================================================================
def _bottom_frame(n: int = 420) -> pd.DataFrame:
    up = list(np.linspace(70, 100, n - 23))
    # v1.4.0 pivot floor gate requires a confirmed swing low BELOW the
    # eventual hook close inside the trailing 60-bar lookback. Insert a 7-bar
    # V into the up-trend at a fixed offset before the dip — well within the
    # pivot lookback regardless of n.
    v_idx = len(up) - 21  # ~17 bars before the shelf starts
    up[v_idx:v_idx + 7] = [98.0, 96.0, 95.0, 94.0, 95.0, 96.0, 98.0]
    shelf = [100, 102, 100, 102, 99, 101, 100, 102, 99, 101, 100, 102]
    dip = [99, 97, 95, 94]
    hook = [96.5]
    closes = up + shelf + dip + hook
    idx = pd.date_range("2021-01-04", periods=len(closes), freq="B", tz="UTC")
    df = pd.DataFrame(
        {
            "open": closes,
            "high": [c + 1 for c in closes],
            "low": [c - 1 for c in closes],
            "close": closes,
            "adj_close": closes,
            "volume": [1_000_000] * len(closes),
            "symbol": ["X"] * len(closes),
        },
        index=idx,
    )
    df.attrs["symbol"] = "X"
    return df


def test_compute_score_is_tail_invariant():
    full = _bottom_frame(420)
    as_of = full.index[-1].date()
    strat = ReversalSwing()
    s_full = strat.reversal_score(full, as_of)
    s_tail = strat.reversal_score(full.tail(240), as_of)
    assert s_full is not None and s_tail is not None
    assert s_tail.score == s_full.score  # identical: trailing-window indicators


# ======================================================================
# 3. sector_rs run-scoped caching
# ======================================================================
def test_sector_rs_caches_map_and_composite(monkeypatch):
    srs.clear_cache()
    calls = {"map": 0, "bars": 0}

    comp_idx = pd.date_range("2021-01-04", periods=300, freq="B", tz="UTC")
    comp_df = pd.DataFrame({"close": np.linspace(100, 130, 300)}, index=comp_idx)

    def fake_sector_map(**_):
        calls["map"] += 1
        return {"AAPL": "Technology"}

    def fake_composite_symbol(sector, **_):
        return f"$EWSECTOR:{sector.upper()}"

    def fake_get_bars(symbol, start=None, end=None, **_):
        calls["bars"] += 1
        return comp_df

    monkeypatch.setattr("stockscan.sectors.store.sector_map", fake_sector_map)
    monkeypatch.setattr("stockscan.sectors.composite.composite_symbol", fake_composite_symbol)
    monkeypatch.setattr("stockscan.data.store.get_bars", fake_get_bars)

    # Many (symbol, day) lookups — the inner-loop pattern.
    for d in pd.date_range("2022-01-03", periods=50, freq="B"):
        comp = srs._composite_symbol_for("AAPL")
        assert comp == "$EWSECTOR:TECHNOLOGY"
        out = srs._composite_closes(comp, d.date())
        assert out is not None and not out.empty

    assert calls["map"] == 1, "sector map should be fetched once per run"
    assert calls["bars"] == 1, "each composite should be fetched once per run"

    # clear_cache forces a refetch (used per backtest run / after composite rebuild).
    srs.clear_cache()
    srs._composite_symbol_for("AAPL")
    assert calls["map"] == 2
    srs.clear_cache()  # leave clean for other tests
