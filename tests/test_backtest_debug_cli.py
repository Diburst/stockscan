"""`stockscan backtest debug STRATEGY SYMBOL` — the per-day signal replay.

Runs the CLI command end-to-end against a synthetic smooth uptrend (no DB) by
monkeypatching ``get_bars`` and pinning the sector-relative return to zero (no
composite in a single-symbol run). Asserts the tool runs, writes a CSV with
one row per trading day, and that the strategy's metadata keys appear as
columns on the days it fired.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from typer.testing import CliRunner

from stockscan.cli import app

MOMENTUM_KEYS = (
    "closeness_52w", "slope_quality", "residual_return_12m", "residual_tilt",
    "realized_vol_1y", "sma_50", "sma_200",
)


def _uptrend(n: int = 700, seed: int = 7) -> pd.DataFrame:
    """A smooth 40%/yr climb with light noise: above its 200-day SMA, close
    to its 52-week high, no gaps — momentum_52w_high fires every Wednesday."""
    rng = np.random.default_rng(seed)
    closes = 100.0 * np.exp(np.arange(n) * (0.40 / 252)) * (1 + rng.normal(0, 0.003, n))
    idx = pd.date_range("2021-01-04", periods=n, freq="B", tz="UTC")
    df = pd.DataFrame(
        {"open": closes, "high": closes * 1.005, "low": closes * 0.995,
         "close": closes, "adj_close": closes, "volume": [1_000_000] * n,
         "symbol": ["TSLA"] * n},
        index=idx,
    )
    df.attrs["symbol"] = "TSLA"
    return df


@pytest.fixture
def _patched(monkeypatch):
    df = _uptrend()
    monkeypatch.setattr(
        "stockscan.data.store.get_bars",
        lambda symbol, start, end, *a, **k: (df if symbol == "TSLA" else pd.DataFrame()),
    )
    # No sector composite in a single-symbol run → residual momentum is neutral.
    monkeypatch.setattr(
        "stockscan.strategies.momentum_52w.sector_relative_return",
        lambda *a, **k: 0.0,
    )
    return df


def _run(*extra: str):
    return CliRunner().invoke(
        app,
        ["backtest", "debug", "momentum_52w_high", "TSLA",
         "--from", "2023-01-02", "--to", "2023-06-30", *extra],
    )


def test_debug_writes_one_row_per_trading_day(_patched, tmp_path):
    csv = tmp_path / "tsla_debug.csv"
    res = _run("--out", str(csv))
    assert res.exit_code == 0, res.output
    assert csv.exists()

    out = pd.read_csv(csv)
    days = _patched.index.date
    n_days = int(((days >= pd.Timestamp("2023-01-02").date()) & (days <= pd.Timestamp("2023-06-30").date())).sum())
    assert len(out) == n_days
    for col in ("date", "close", "fired", "score", *MOMENTUM_KEYS):
        assert col in out.columns


def test_debug_fired_days_are_review_days_with_metadata(_patched, tmp_path):
    csv = tmp_path / "out.csv"
    res = _run("--out", str(csv))
    assert res.exit_code == 0, res.output
    out = pd.read_csv(csv, parse_dates=["date"])

    fired = out[out["fired"]]
    assert len(fired) > 0
    # Entries only on the strategy's weekly review day (Wednesday).
    assert (fired["date"].dt.weekday == 2).all()
    # Every fired row carries a score and the strategy's own inputs.
    assert fired["score"].notna().all()
    assert fired["closeness_52w"].notna().all()
    assert (fired["closeness_52w"] >= 0.90).all()
    # Days that did not fire carry no score or metadata.
    quiet = out[~out["fired"]]
    assert quiet["score"].isna().all()
    assert quiet["closeness_52w"].isna().all()


def test_debug_table_filters_to_fired_days_unless_all(_patched):
    res = _run()
    assert res.exit_code == 0, res.output
    assert "per-day signal replay" in res.output
    assert "signal days" in res.output

    res_all = _run("--all")
    assert res_all.exit_code == 0, res_all.output
    assert len(res_all.output) > len(res.output)


def test_debug_unknown_strategy_exits_nonzero(_patched):
    res = CliRunner().invoke(app, ["backtest", "debug", "no_such_strategy", "TSLA"])
    assert res.exit_code == 1
