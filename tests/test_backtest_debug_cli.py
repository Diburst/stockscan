"""`stockscan backtest debug SYMBOL` — the per-day reversal-score inspector.

Runs the CLI command end-to-end against a synthetic sawtooth (no DB) by
monkeypatching ``get_bars`` and making relative strength abstain (as it does in a
single-symbol run with no composite). Asserts the tool runs, writes a
full-precision CSV with the expected schema, and reports the score breakdown.

It also guards the fix for the "only-one-trade" single-symbol backtests: the
pivot_proximity 3-bar approach window lets the level still register on the turn's
hook bar, so clean V-bottoms now clear entry_threshold (was: 0 entries on a
sawtooth). See signal_scoring_spec.md §6 calibration note (2026-05).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from typer.testing import CliRunner

from stockscan.cli import app


def _sawtooth(n_cycles: int = 22, down: int = 12, up: int = 12,
              base: float = 200.0, amp: float = 40.0, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    closes, vols = [], []
    for c in range(n_cycles):
        drift = c * 2.0
        top, bot = base + drift + amp / 2, base + drift - amp / 2
        closes += list(np.linspace(top, bot, down + 1))[1:]
        closes += list(np.linspace(bot, top, up + 1))[1:]
        vols += [1_000_000] * (down - 1) + [3_000_000]
        vols += [1_000_000] * (up - 1) + [2_500_000]
    closes = np.array(closes) + rng.normal(0, 0.3, len(closes))
    n = len(closes)
    idx = pd.date_range("2022-01-03", periods=n, freq="B", tz="UTC")
    df = pd.DataFrame(
        {"open": closes, "high": closes + 1.0, "low": closes - 1.0,
         "close": closes, "adj_close": closes, "volume": vols,
         "symbol": ["TSLA"] * n},
        index=idx,
    )
    df.attrs["symbol"] = "TSLA"
    return df


@pytest.fixture
def _patched(monkeypatch):
    df = _sawtooth()
    monkeypatch.setattr(
        "stockscan.data.store.get_bars",
        lambda symbol, start, end, *a, **k: (df if symbol == "TSLA" else pd.DataFrame()),
    )
    # No sector composite in a single-symbol run → relative strength abstains cleanly.
    monkeypatch.setattr(
        "stockscan.indicators.relative_strength._composite_symbol_for",
        lambda *a, **k: None,
    )
    return df


def test_debug_runs_and_writes_csv(_patched, tmp_path):
    csv = tmp_path / "tsla_debug.csv"
    res = CliRunner().invoke(
        app,
        ["backtest", "debug", "reversal_swing", "TSLA",
         "--from", "2023-01-02", "--to", "2024-01-10",
         "--out", str(csv)],
    )
    assert res.exit_code == 0, res.output
    assert csv.exists()

    out = pd.read_csv(csv)
    for col in ("date", "close", "score", "D", "C", "reversal_trigger",
                "pivot_proximity", "trend_location", "volume_confirm", "decision"):
        assert col in out.columns
    # The score is computed on the hook days (positive trigger). With the v1.2.0
    # gate, "no-turn" days legitimately return None, so most days are blank —
    # what we want to verify is that the hook days actually produced a real
    # number, not that "most days have a score."
    assert out["score"].notna().sum() > 0


def test_debug_bottoms_now_enter(_patched, tmp_path):
    """After the v1.2.0 gate: the turn and the level co-occur on V-bottoms, so
    entries fire on the hook day. (The pre-v1.2.0 test also asserted ``top``
    days were flagged — that branch is unreachable now because top setups
    return None from reversal_score, so the debug command never marks a day
    as ``top``. Stops and time-stop carry exits.)"""
    csv = tmp_path / "out.csv"
    res = CliRunner().invoke(
        app,
        ["backtest", "debug", "reversal_swing", "TSLA",
         "--from", "2023-01-02", "--to", "2024-01-10",
         "--out", str(csv)],
    )
    assert res.exit_code == 0, res.output
    out = pd.read_csv(csv)

    rt = out["reversal_trigger"].fillna(0.0)
    pv = out["pivot_proximity"].fillna(0.0)
    turn = rt >= 0.5            # the bounce/hook fired
    level = pv >= 0.3           # price at a support level
    # The two bottom signals line up on the same day at least once...
    assert (turn & level).sum() > 0
    # ...so bottoms actually generate entries.
    assert (out["decision"] == "ENTER").sum() > 0
    # And the gate prevents any day from being flagged as a top (the score
    # is None for top setups, so the "top" decision branch is never entered).
    assert (out["decision"] == "top").sum() == 0
