"""Behavioral tests for the Reversal Swing strategy.

The contract tests in test_strategy_contract.py run against this strategy
automatically (idempotence, no-look-ahead, valid required_history). Here we
test the score→signal wiring and the exit policy (reversal-top / hard stop /
time stop). Thresholds are set *relative* to the measured reversal score so the
tests don't depend on its exact magnitude (which the indicator/composite tests
cover) — only on the "fires iff score ≥ threshold" mechanism.

sector_rs abstains here (no DB), so the score is built from reversal_trigger +
pivot_proximity + trend_location × volume_confirm, which is deterministic.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import numpy as np
import pandas as pd

from stockscan.strategies import STRATEGY_REGISTRY, PositionSnapshot, discover_strategies
from stockscan.strategies.reversal_swing import ReversalSwing, ReversalSwingParams
from stockscan.technical.score import compute_technical_score


def _bars(closes: list[float]) -> pd.DataFrame:
    n = len(closes)
    idx = pd.date_range("2022-06-01", periods=n, freq="B", tz="UTC")
    df = pd.DataFrame(
        {
            "open": closes,
            "high": [c + 1 for c in closes],
            "low": [c - 1 for c in closes],
            "close": closes,
            "adj_close": closes,
            "volume": [1_000_000] * n,
            "symbol": ["TEST"] * n,
        },
        index=idx,
    )
    df.attrs["symbol"] = "TEST"
    return df


def _bottom_bars() -> pd.DataFrame:
    up = list(np.linspace(70, 100, 216))
    shelf = [100, 102, 100, 102, 99, 101, 100, 102, 99, 101, 100, 102]
    dip = [99, 97, 95, 94]
    hook = [96.5]
    return _bars(up + shelf + dip + hook)  # 233 bars, positive reversal score


def _top_bars() -> pd.DataFrame:
    dn = list(np.linspace(130, 100, 216))
    shelf = [100, 98, 100, 98, 101, 99, 100, 98, 101, 99, 100, 98]
    rip = [101, 103, 105, 106]
    hookd = [103.5]
    return _bars(dn + shelf + rip + hookd)  # 233 bars, negative reversal score


def test_registered():
    discover_strategies()
    assert "reversal_swing" in STRATEGY_REGISTRY._by_name


def test_required_history():
    assert ReversalSwing(ReversalSwingParams()).required_history() == 230


def test_fires_long_on_a_confirmed_bottom():
    b = _bottom_bars()
    as_of = b.index[-1].date()
    score = float(compute_technical_score(ReversalSwing, b, as_of).score)
    assert score > 0.07  # it's a bottom (and clear of the 0.05 param floor)

    fires = ReversalSwing(ReversalSwingParams(entry_threshold=round(score - 0.02, 4)))
    sigs = fires.signals(b, as_of)
    assert len(sigs) == 1
    sig = sigs[0]
    assert sig.side == "long"
    assert sig.suggested_stop < sig.suggested_entry
    assert sig.metadata["setup_type"] in {"dip_in_uptrend", "counter_trend_bottom"}
    assert float(sig.score) == round(score, 4)

    # Raise the bar above the score → no entry.
    quiet = ReversalSwing(ReversalSwingParams(entry_threshold=round(score + 0.02, 4)))
    assert quiet.signals(b, as_of) == []


def test_no_signal_below_required_history():
    b = _bottom_bars().iloc[:100]  # < 230 bars
    s = ReversalSwing(ReversalSwingParams(entry_threshold=0.05))
    assert s.signals(b, b.index[-1].date()) == []


def _recent_pos(b: pd.DataFrame, avg_cost: float, hold_bars: int) -> PositionSnapshot:
    opened = b.index[-1 - hold_bars].to_pydatetime().astimezone(timezone.utc)
    return PositionSnapshot(
        symbol="TEST", qty=100, avg_cost=Decimal(str(avg_cost)), opened_at=opened,
        strategy="reversal_swing",
    )


def test_exit_on_confirmed_top():
    b = _top_bars()
    as_of = b.index[-1].date()
    score = float(compute_technical_score(ReversalSwing, b, as_of).score)
    assert score < -0.07  # it's a top

    pos = _recent_pos(b, avg_cost=90.0, hold_bars=2)  # held 2 bars (no time stop), low cost (no hard stop)
    fires = ReversalSwing(ReversalSwingParams(exit_threshold=round(abs(score) - 0.02, 4)))
    d = fires.exit_rules(pos, b, as_of)
    assert d is not None and d.reason == "reversal_top"

    quiet = ReversalSwing(ReversalSwingParams(exit_threshold=round(abs(score) + 0.02, 4)))
    assert quiet.exit_rules(pos, b, as_of) is None


def test_exit_on_time_stop():
    b = _bottom_bars()
    as_of = b.index[-1].date()
    pos = _recent_pos(b, avg_cost=50.0, hold_bars=230)  # held well beyond max_holding_days
    # High exit_threshold so the reversal-top branch can't fire; low cost so no hard stop.
    s = ReversalSwing(ReversalSwingParams(exit_threshold=0.95, max_holding_days=20))
    d = s.exit_rules(pos, b, as_of)
    assert d is not None and d.reason == "time_stop"


def test_exit_on_hard_stop():
    b = _bottom_bars()  # last close ~96.5
    as_of = b.index[-1].date()
    pos = _recent_pos(b, avg_cost=120.0, hold_bars=2)  # bought high, held briefly
    s = ReversalSwing(ReversalSwingParams(exit_threshold=0.95))  # block reversal_top branch
    d = s.exit_rules(pos, b, as_of)
    assert d is not None and d.reason == "hard_stop"


def test_signals_idempotent():
    b = _bottom_bars()
    as_of = b.index[-1].date()
    s = ReversalSwing(ReversalSwingParams(entry_threshold=0.05))
    assert s.signals(b.copy(), as_of) == s.signals(b.copy(), as_of)
