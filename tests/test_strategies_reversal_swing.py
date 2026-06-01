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
from stockscan.strategies.reversal_swing import ReversalSwing


def _strat(**knobs) -> ReversalSwing:
    """Build a strategy instance and override any ClassVar knobs per-instance.

    Setting attributes on the instance shadows the ClassVar without polluting
    the registered class — the right shape for parametrized tests now that
    knobs aren't a pydantic params bag.
    """
    s = ReversalSwing()
    for k, v in knobs.items():
        setattr(s, k, v)
    return s


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
    # v1.4.0 added the pivot_proximity floor gate, so the fixture needs a
    # confirmed swing low BELOW the eventual hook close inside the trailing
    # 60-bar lookback. Insert a 7-bar V into the up-trend close enough to the
    # eventual hook close that pivot_proximity registers positive.
    up = list(np.linspace(70, 100, 216))
    # V-shape at indices 195-201: confirmed swing low at idx 198 (close 94,
    # low 93) with k=3 higher lows on each side.
    up[195:202] = [98.0, 96.0, 95.0, 94.0, 95.0, 96.0, 98.0]
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
    assert ReversalSwing().required_history() == 230


def test_fires_long_on_a_confirmed_bottom():
    b = _bottom_bars()
    as_of = b.index[-1].date()
    score = float(ReversalSwing().reversal_score(b, as_of).score)
    assert score > 0.07  # it's a bottom (and clear of the 0.05 param floor)

    fires = _strat(entry_threshold=round(score - 0.02, 4))
    sigs = fires.signals(b, as_of)
    assert len(sigs) == 1
    sig = sigs[0]
    assert sig.side == "long"
    assert sig.suggested_stop < sig.suggested_entry
    assert sig.metadata["setup_type"] in {"dip_in_uptrend", "counter_trend_bottom"}
    assert float(sig.score) == round(score, 4)

    # Raise the bar above the score → no entry.
    quiet = _strat(entry_threshold=round(score + 0.02, 4))
    assert quiet.signals(b, as_of) == []


def test_no_signal_below_required_history():
    b = _bottom_bars().iloc[:100]  # < 230 bars
    s = _strat(entry_threshold=0.05)
    assert s.signals(b, b.index[-1].date()) == []


def _recent_pos(b: pd.DataFrame, avg_cost: float, hold_bars: int) -> PositionSnapshot:
    opened = b.index[-1 - hold_bars].to_pydatetime().astimezone(timezone.utc)
    return PositionSnapshot(
        symbol="TEST", qty=100, avg_cost=Decimal(str(avg_cost)), opened_at=opened,
        strategy="reversal_swing",
    )


def test_top_setup_no_longer_fires_reversal_top_exit():
    """v1.2.0 architectural change. The hard gate on reversal_trigger > 0 in
    reversal_score() makes top setups return None — and the reversal_top exit
    branch in exit_rules() needed a negative score to fire. So:

      1. reversal_score() returns None on a top fixture.
      2. exit_rules() no longer fires reversal_top, regardless of how loose
         the exit_threshold is set.

    Exits are now carried by the ATR hard stop and the time stop only. See
    ReversalSwing.manual ("Hard gate" section) for the rationale and the
    bt20 evidence that motivated this."""
    b = _top_bars()
    as_of = b.index[-1].date()
    assert ReversalSwing().reversal_score(b, as_of) is None

    # Held briefly, low avg_cost so no hard_stop, exit_threshold floored so
    # reversal_top would fire on any negative score — but the gate prevents
    # that score from ever being computed.
    pos = _recent_pos(b, avg_cost=90.0, hold_bars=2)
    s = _strat(exit_threshold=0.05)
    d = s.exit_rules(pos, b, as_of)
    assert d is None or d.reason != "reversal_top"


def test_exit_on_time_stop():
    b = _bottom_bars()
    as_of = b.index[-1].date()
    pos = _recent_pos(b, avg_cost=50.0, hold_bars=230)  # held well beyond max_holding_days
    # High exit_threshold so the reversal-top branch can't fire; low cost so no hard stop.
    s = _strat(exit_threshold=0.95, max_holding_days=20)
    d = s.exit_rules(pos, b, as_of)
    assert d is not None and d.reason == "time_stop"


def test_exit_on_hard_stop():
    b = _bottom_bars()  # last close ~96.5
    as_of = b.index[-1].date()
    pos = _recent_pos(b, avg_cost=120.0, hold_bars=2)  # bought high, held briefly
    s = _strat(exit_threshold=0.95)  # block reversal_top branch
    d = s.exit_rules(pos, b, as_of)
    assert d is not None and d.reason == "hard_stop"


def test_signals_idempotent():
    b = _bottom_bars()
    as_of = b.index[-1].date()
    s = _strat(entry_threshold=0.05)
    assert s.signals(b.copy(), as_of) == s.signals(b.copy(), as_of)
