"""RSI(2) pullback (rsi2_meanrev v2.0.0) behavioral tests.

The two sector legs (``sector_return`` / ``sector_relative_return``) are
module-level names in ``stockscan.strategies.rsi2_meanrev`` and are patched
so no composite bars or DB are touched.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from stockscan.indicators import rsi, sma
from stockscan.strategies import PositionSnapshot
from stockscan.strategies.rsi2_meanrev import RSI2MeanReversion

MODULE = "stockscan.strategies.rsi2_meanrev"


# ---------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------
def _make_bars(
    closes: list[float],
    *,
    volume: list[float] | None = None,
    symbol: str = "TEST",
    start: str = "2023-01-02",
) -> pd.DataFrame:
    n = len(closes)
    idx = pd.date_range(start, periods=n, freq="B", tz="UTC")
    df = pd.DataFrame(
        {
            "open": closes,
            "high": [c * 1.005 for c in closes],
            "low": [c * 0.995 for c in closes],
            "close": closes,
            "adj_close": closes,
            "volume": volume if volume is not None else [1_000_000.0] * n,
        },
        index=idx,
    )
    df.attrs["symbol"] = symbol
    return df


def _pullback_closes(n_up: int = 260, drop: float = 0.06) -> list[float]:
    """Long uptrend then a two-day sharp drop: RSI(2) ≈ 0, still > SMA(200)."""
    up = np.linspace(100.0, 200.0, n_up).tolist()
    top = up[-1]
    return up + [top * (1 - drop / 2), top * (1 - drop)]


@pytest.fixture
def strategy() -> RSI2MeanReversion:
    return RSI2MeanReversion()


@pytest.fixture
def sector_ok(monkeypatch):
    """Sector flat over the month, stock down 6% → relative −6%."""
    monkeypatch.setattr(f"{MODULE}.sector_return", lambda bars, as_of, *, lookback: 0.0)
    monkeypatch.setattr(
        f"{MODULE}.sector_relative_return", lambda bars, as_of, *, lookback: -0.06
    )


def _as_of(bars: pd.DataFrame) -> date:
    return bars.index[-1].date()


# ---------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------
def test_fires_on_pullback_in_uptrend(strategy, sector_ok):
    bars = _make_bars(_pullback_closes())
    price = bars["adj_close"]
    # Sanity on the fixture itself: setup conditions hold.
    assert price.iloc[-1] > sma(price, 200).iloc[-1]
    assert rsi(price, 2).iloc[-1] < 10

    sigs = strategy.signals(bars, as_of=_as_of(bars))
    assert len(sigs) == 1
    sig = sigs[0]
    assert sig.strategy_name == "rsi2_meanrev"
    assert sig.strategy_version == "2.0.0"
    assert sig.symbol == "TEST"
    assert sig.side == "long"
    assert sig.suggested_entry == Decimal(str(round(float(bars["close"].iloc[-1]), 4)))
    assert sig.metadata["rsi_2"] < 10
    assert sig.metadata["sector_return_1m"] == 0.0
    assert sig.metadata["relative_volume"] == pytest.approx(1.0)


def test_no_signal_below_sma200(strategy, sector_ok):
    closes = np.linspace(200.0, 80.0, 262).tolist()  # RSI(2) is ~0 but below trend
    bars = _make_bars(closes)
    assert strategy.signals(bars, as_of=_as_of(bars)) == []


def test_no_signal_when_rsi_not_oversold(strategy, sector_ok):
    closes = np.linspace(100.0, 200.0, 262).tolist()  # pure uptrend, RSI(2) ≈ 100
    bars = _make_bars(closes)
    assert strategy.signals(bars, as_of=_as_of(bars)) == []


def test_no_signal_when_sector_in_downtrend(strategy, monkeypatch):
    monkeypatch.setattr(f"{MODULE}.sector_return", lambda bars, as_of, *, lookback: -0.06)
    monkeypatch.setattr(
        f"{MODULE}.sector_relative_return", lambda bars, as_of, *, lookback: -0.01
    )
    bars = _make_bars(_pullback_closes())
    assert strategy.signals(bars, as_of=_as_of(bars)) == []


def test_sector_exactly_at_floor_still_fires(strategy, monkeypatch):
    monkeypatch.setattr(f"{MODULE}.sector_return", lambda bars, as_of, *, lookback: -0.05)
    monkeypatch.setattr(
        f"{MODULE}.sector_relative_return", lambda bars, as_of, *, lookback: -0.02
    )
    bars = _make_bars(_pullback_closes())
    assert len(strategy.signals(bars, as_of=_as_of(bars))) == 1


def test_no_signal_when_sector_unavailable(strategy, monkeypatch):
    monkeypatch.setattr(f"{MODULE}.sector_return", lambda bars, as_of, *, lookback: None)
    monkeypatch.setattr(
        f"{MODULE}.sector_relative_return", lambda bars, as_of, *, lookback: None
    )
    bars = _make_bars(_pullback_closes())
    assert strategy.signals(bars, as_of=_as_of(bars)) == []


def test_no_signal_when_selloff_volume_heavy(strategy, sector_ok):
    closes = _pullback_closes()
    n = len(closes)
    volume = [1_000_000.0] * (n - 2) + [1_500_000.0, 1_500_000.0]  # exactly 1.5×
    bars = _make_bars(closes, volume=volume)
    assert strategy.signals(bars, as_of=_as_of(bars)) == []


def test_quiet_selloff_volume_passes(strategy, sector_ok):
    closes = _pullback_closes()
    n = len(closes)
    volume = [1_000_000.0] * (n - 2) + [1_400_000.0, 1_400_000.0]
    bars = _make_bars(closes, volume=volume)
    sigs = strategy.signals(bars, as_of=_as_of(bars))
    assert len(sigs) == 1
    assert sigs[0].metadata["relative_volume"] == pytest.approx(1.4)


def test_score_is_idiosyncratic_drop(strategy, monkeypatch):
    """score = −(stock 1m − sector 1m) = sector − stock."""
    monkeypatch.setattr(f"{MODULE}.sector_return", lambda bars, as_of, *, lookback: 0.02)
    monkeypatch.setattr(
        f"{MODULE}.sector_relative_return", lambda bars, as_of, *, lookback: -0.0875
    )
    bars = _make_bars(_pullback_closes())
    sig = strategy.signals(bars, as_of=_as_of(bars))[0]
    assert sig.score == Decimal("0.0875")
    assert sig.metadata["idiosyncratic_drop"] == pytest.approx(0.0875)
    assert sig.metadata["stock_return_1m"] == pytest.approx(-0.0675)
    assert sig.metadata["sector_return_1m"] == pytest.approx(0.02)


def test_sector_legs_receive_view_and_lookback(strategy, monkeypatch):
    seen: dict[str, object] = {}

    def fake_sector(bars, as_of, *, lookback):
        seen["as_of"] = as_of
        seen["lookback"] = lookback
        seen["symbol"] = bars.attrs.get("symbol")
        return 0.0

    monkeypatch.setattr(f"{MODULE}.sector_return", fake_sector)
    monkeypatch.setattr(
        f"{MODULE}.sector_relative_return", lambda bars, as_of, *, lookback: -0.05
    )
    bars = _make_bars(_pullback_closes())
    strategy.signals(bars, as_of=_as_of(bars))
    assert seen == {"as_of": _as_of(bars), "lookback": 21, "symbol": "TEST"}


def test_no_price_stop(strategy, sector_ok):
    bars = _make_bars(_pullback_closes())
    sig = strategy.signals(bars, as_of=_as_of(bars))[0]
    assert sig.suggested_stop is None
    assert RSI2MeanReversion.position_pct == 0.10
    assert RSI2MeanReversion.sizes_down_in_high_vol is False


def test_require_hook_blocks_down_close(sector_ok):
    class _Hooked(RSI2MeanReversion):
        name = "rsi2_hooked_test"
        version = "0.0.0-test"
        require_hook = True

    bars = _make_bars(_pullback_closes())  # last close < previous close
    assert _Hooked().signals(bars, as_of=_as_of(bars)) == []
    assert RSI2MeanReversion().signals(bars, as_of=_as_of(bars)) != []


def test_require_hook_allows_up_close(sector_ok):
    class _Hooked(RSI2MeanReversion):
        name = "rsi2_hooked_up_test"
        version = "0.0.0-test"
        require_hook = True

    closes = _pullback_closes(drop=0.08)
    closes.append(closes[-1] * 1.002)  # a tiny hook; RSI(2) stays < 10
    bars = _make_bars(closes)
    assert rsi(bars["adj_close"], 2).iloc[-1] < 10
    assert len(_Hooked().signals(bars, as_of=_as_of(bars))) == 1


def test_signals_no_lookahead(strategy, sector_ok):
    closes = _pullback_closes() + np.linspace(190.0, 230.0, 30).tolist()
    bars = _make_bars(closes)
    as_of = bars.index[261].date()
    truncated = bars[bars.index.date <= as_of]
    truncated.attrs["symbol"] = "TEST"
    assert strategy.signals(bars, as_of=as_of) == strategy.signals(truncated, as_of=as_of)
    assert len(strategy.signals(bars, as_of=as_of)) == 1


def test_required_history(strategy):
    assert strategy.required_history() == 200 + 50 + 2
    bars = _make_bars(_pullback_closes(n_up=240))  # 242 bars < 252
    assert strategy.signals(bars, as_of=_as_of(bars)) == []


# ---------------------------------------------------------------------
# Exits
# ---------------------------------------------------------------------
def _position(bars: pd.DataFrame, opened_idx: int, cost: float = 100.0) -> PositionSnapshot:
    return PositionSnapshot(
        symbol="TEST",
        qty=10,
        avg_cost=Decimal(str(cost)),
        opened_at=bars.index[opened_idx].to_pydatetime(),
        strategy="rsi2_meanrev",
    )


def test_exit_recovered_above_sma5(strategy):
    closes = [100.0] * 20 + [101.0]
    bars = _make_bars(closes)
    decision = strategy.exit_rules(_position(bars, 18), bars, as_of=_as_of(bars))
    assert decision is not None
    assert decision.reason == "recovered_above_sma5"
    assert decision.qty == 10


def test_exit_rsi_recovered(strategy):
    # Slide, a hard drop, then two up-days that leave the close under the
    # SMA(5): RSI(2) recovers past 50 before the SMA exit fires.
    closes = [110.0 - i for i in range(11)] + [88.0, 91.0, 94.0]
    bars = _make_bars(closes)
    price = bars["adj_close"]
    assert price.iloc[-1] < sma(price, 5).iloc[-1]
    assert rsi(price, 2).iloc[-1] > 50
    decision = strategy.exit_rules(_position(bars, 10), bars, as_of=_as_of(bars))
    assert decision is not None
    assert decision.reason == "rsi_recovered"


def test_exit_time_stop_after_ten_bars(strategy):
    closes = [100.0 - 0.1 * i for i in range(30)]  # gentle slide: no SMA/RSI exit
    bars = _make_bars(closes)
    ten_after = _position(bars, len(bars) - 11)  # 10 bars after open
    decision = strategy.exit_rules(ten_after, bars, as_of=_as_of(bars))
    assert decision is not None
    assert decision.reason == "time_stop"

    nine_after = _position(bars, len(bars) - 10)
    assert strategy.exit_rules(nine_after, bars, as_of=_as_of(bars)) is None


def test_exit_hold_otherwise(strategy):
    closes = [100.0] * 15 + [110.0, 108.0, 106.0, 104.0, 102.0, 100.0]  # falling
    bars = _make_bars(closes)
    assert strategy.exit_rules(_position(bars, 18), bars, as_of=_as_of(bars)) is None


def test_exit_rules_no_lookahead(strategy):
    closes = [100.0 - 0.1 * i for i in range(20)] + [105.0] + [150.0] * 5
    bars = _make_bars(closes)
    as_of = bars.index[19].date()  # sliding through here: hold
    assert strategy.exit_rules(_position(bars, 18), bars, as_of=as_of) is None
    as_of = bars.index[20].date()
    out = strategy.exit_rules(_position(bars, 18), bars, as_of=as_of)
    assert out is not None and out.reason == "recovered_above_sma5"
