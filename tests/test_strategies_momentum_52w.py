"""52-week-high momentum (momentum_52w_high v2.0.0) behavioral tests.

``sector_relative_return`` is a module-level name in
``stockscan.strategies.momentum_52w`` and is patched so no composite bars or
DB are touched.
"""

from __future__ import annotations

import math
from datetime import date
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from stockscan.indicators import sma
from stockscan.strategies import PositionSnapshot
from stockscan.strategies.momentum_52w import Momentum52WeekHigh

MODULE = "stockscan.strategies.momentum_52w"

# 2023-01-02 is a Monday and ``freq="B"`` skips no holidays, so bar ``i``
# falls on weekday ``i % 5``. 298 bars end on a Wednesday (297 % 5 == 2).
START = "2023-01-02"
N_WED = 298


# ---------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------
def _make_bars(closes, *, symbol: str = "TEST") -> pd.DataFrame:
    closes = [float(c) for c in closes]
    n = len(closes)
    idx = pd.date_range(START, periods=n, freq="B", tz="UTC")
    df = pd.DataFrame(
        {
            "open": closes,
            "high": [c * 1.005 for c in closes],
            "low": [c * 0.995 for c in closes],
            "close": closes,
            "adj_close": closes,
            "volume": [1_000_000.0] * n,
        },
        index=idx,
    )
    df.attrs["symbol"] = symbol
    return df


def _smooth_climb(n: int = N_WED, total_log_return: float = 0.5) -> np.ndarray:
    """Exponential, perfectly smooth climb: passes every eligibility gate."""
    return 100.0 * np.exp(np.linspace(0.0, total_log_return, n))


def _as_of(bars: pd.DataFrame) -> date:
    return bars.index[-1].date()


@pytest.fixture
def strategy() -> Momentum52WeekHigh:
    return Momentum52WeekHigh()


@pytest.fixture
def no_residual(monkeypatch):
    monkeypatch.setattr(
        f"{MODULE}.sector_relative_return", lambda bars, as_of, *, lookback: 0.0
    )


# ---------------------------------------------------------------------
# Review day
# ---------------------------------------------------------------------
def test_fires_on_wednesday(strategy, no_residual):
    bars = _make_bars(_smooth_climb())
    as_of = _as_of(bars)
    assert as_of.weekday() == 2

    sigs = strategy.signals(bars, as_of=as_of)
    assert len(sigs) == 1
    sig = sigs[0]
    assert sig.strategy_name == "momentum_52w_high"
    assert sig.strategy_version == "2.0.0"
    assert sig.symbol == "TEST"
    assert sig.side == "long"
    assert sig.suggested_entry == Decimal(str(round(float(bars["close"].iloc[-1]), 4)))
    assert sig.metadata["closeness_52w"] == pytest.approx(1.0)
    assert sig.metadata["residual_tilt"] == 0.0
    assert sig.metadata["sma_50"] > sig.metadata["sma_200"]


@pytest.mark.parametrize("extra", [1, 2, 3, 4], ids=["thu", "fri", "mon", "tue"])
def test_no_signal_off_review_day(strategy, no_residual, extra):
    bars = _make_bars(_smooth_climb(N_WED + extra))
    as_of = _as_of(bars)
    assert as_of.weekday() != 2
    assert strategy.signals(bars, as_of=as_of) == []
    # The same setup on the preceding Wednesday does fire.
    wed = bars.index[N_WED - 1].date()
    assert wed.weekday() == 2
    assert len(strategy.signals(bars, as_of=wed)) == 1


# ---------------------------------------------------------------------
# Eligibility gates
# ---------------------------------------------------------------------
def test_no_signal_below_sma200(strategy, no_residual):
    bars = _make_bars(np.linspace(200.0, 80.0, N_WED))
    assert strategy.signals(bars, as_of=_as_of(bars)) == []


def test_no_signal_when_sma50_below_sma200(strategy, no_residual):
    # A V: long rise, two-month slide, sharp recovery to a new high. Price is
    # back above the 200-day and at its 52-week high, but the 50-day still
    # sits under the 200-day.
    closes = (
        list(np.linspace(100.0, 200.0, 200))
        + list(np.linspace(195.0, 140.0, 60))
        + list(np.linspace(145.0, 205.0, 38))
    )
    bars = _make_bars(closes)
    price = bars["adj_close"]
    assert price.iloc[-1] > sma(price, 200).iloc[-1]
    assert sma(price, 50).iloc[-1] < sma(price, 200).iloc[-1]
    assert price.iloc[-1] / price.iloc[-252:].max() >= 0.90
    assert strategy.signals(bars, as_of=_as_of(bars)) == []


def test_gap_screen_at_fifteen_percent(strategy, no_residual):
    blown = _smooth_climb()
    blown[-45:] *= 1.16  # one +16% day inside the 90-bar gap window
    bars = _make_bars(blown)
    assert strategy.signals(bars, as_of=_as_of(bars)) == []

    ok = _smooth_climb()
    ok[-45:] *= 1.14  # +14%: under the screen
    bars = _make_bars(ok)
    sigs = strategy.signals(bars, as_of=_as_of(bars))
    assert len(sigs) == 1
    assert sigs[0].metadata["realized_vol_1y"] < 0.60


def test_gap_outside_window_ignored(strategy, no_residual):
    old = _smooth_climb()
    old[-120:] *= 1.30  # +30% day, but 120 bars back — outside the 90-bar window
    bars = _make_bars(old)
    assert len(strategy.signals(bars, as_of=_as_of(bars))) == 1


def test_realized_vol_cap(strategy, no_residual):
    n = N_WED
    signs = np.array([1.0 if i % 2 == 1 else -1.0 for i in range(n)])  # last bar up
    wild = _smooth_climb(n) * (1.0 + 0.05 * signs)  # ±5%/day ≈ 160% annualised
    bars = _make_bars(wild)
    price = bars["adj_close"]
    daily = price.pct_change().iloc[-90:]
    assert daily.abs().max() < 0.15  # gap screen passes
    assert daily.std(ddof=0) * math.sqrt(252) > 0.60
    assert price.iloc[-1] / price.iloc[-252:].max() >= 0.90
    assert strategy.signals(bars, as_of=_as_of(bars)) == []


def test_closeness_gate(strategy, no_residual):
    climb = _smooth_climb()
    closes = list(climb[:-10]) + list(climb[-11] * np.linspace(0.99, 0.89, 10))
    bars = _make_bars(closes)
    price = bars["adj_close"]
    assert price.iloc[-1] > sma(price, 200).iloc[-1]
    assert sma(price, 50).iloc[-1] > sma(price, 200).iloc[-1]
    assert price.iloc[-1] / price.iloc[-252:].max() == pytest.approx(0.89, abs=1e-6)
    assert strategy.signals(bars, as_of=_as_of(bars)) == []

    closes = list(climb[:-10]) + list(climb[-11] * np.linspace(0.99, 0.91, 10))
    bars = _make_bars(closes)
    sigs = strategy.signals(bars, as_of=_as_of(bars))
    assert len(sigs) == 1
    assert sigs[0].metadata["closeness_52w"] == pytest.approx(0.91, abs=1e-6)


def test_required_history(strategy):
    assert strategy.required_history() == 257
    bars = _make_bars(_smooth_climb(N_WED - 45))  # 253 bars, ends on Wednesday
    assert _as_of(bars).weekday() == 2
    assert strategy.signals(bars, as_of=_as_of(bars)) == []


# ---------------------------------------------------------------------
# Score and stop
# ---------------------------------------------------------------------
def _expected_base(bars: pd.DataFrame) -> tuple[float, float]:
    price = bars["adj_close"].astype(float)
    closeness = float(price.iloc[-1]) / float(price.iloc[-252:].max())
    slope_q = Momentum52WeekHigh._slope_quality(price.iloc[-90:])
    return closeness, slope_q


@pytest.mark.parametrize(
    ("residual", "tilt"),
    [(0.10, 0.10), (-0.10, -0.10), (0.60, 0.25), (-0.60, -0.25), (None, 0.0)],
)
def test_score_is_closeness_plus_slope_plus_clipped_tilt(strategy, monkeypatch, residual, tilt):
    monkeypatch.setattr(
        f"{MODULE}.sector_relative_return", lambda bars, as_of, *, lookback: residual
    )
    bars = _make_bars(_smooth_climb())
    sig = strategy.signals(bars, as_of=_as_of(bars))[0]
    closeness, slope_q = _expected_base(bars)
    assert float(sig.score) == pytest.approx(closeness + slope_q + tilt, abs=1e-4)
    assert sig.metadata["residual_tilt"] == pytest.approx(tilt)
    assert sig.metadata["slope_quality"] == pytest.approx(slope_q, abs=1e-4)
    if residual is None:
        assert sig.metadata["residual_return_12m"] is None
    else:
        assert sig.metadata["residual_return_12m"] == pytest.approx(residual)


def test_residual_uses_twelve_month_lookback(strategy, monkeypatch):
    seen: dict[str, object] = {}

    def fake(bars, as_of, *, lookback):
        seen["lookback"] = lookback
        seen["as_of"] = as_of
        seen["symbol"] = bars.attrs.get("symbol")
        return 0.0

    monkeypatch.setattr(f"{MODULE}.sector_relative_return", fake)
    bars = _make_bars(_smooth_climb())
    strategy.signals(bars, as_of=_as_of(bars))
    assert seen == {"lookback": 252, "as_of": _as_of(bars), "symbol": "TEST"}


def test_stop_is_fifteen_percent_below_close(strategy, no_residual):
    bars = _make_bars(_smooth_climb())
    sig = strategy.signals(bars, as_of=_as_of(bars))[0]
    last_close = float(bars["close"].iloc[-1])
    assert sig.suggested_stop == Decimal(str(round(last_close * 0.85, 4)))
    assert sig.suggested_stop < sig.suggested_entry
    assert Momentum52WeekHigh.position_pct is None
    assert Momentum52WeekHigh.default_risk_pct == 0.0075
    assert Momentum52WeekHigh.max_open_positions == 10


def test_signals_no_lookahead(strategy, no_residual):
    closes = list(_smooth_climb()) + [50.0] * 20  # a crash after as_of
    bars = _make_bars(closes)
    as_of = bars.index[N_WED - 1].date()
    truncated = bars[bars.index.date <= as_of]
    truncated.attrs["symbol"] = "TEST"
    a = strategy.signals(bars, as_of=as_of)
    b = strategy.signals(truncated, as_of=as_of)
    assert a == b
    assert len(a) == 1


# ---------------------------------------------------------------------
# Exits
# ---------------------------------------------------------------------
def _position(bars: pd.DataFrame, cost: float) -> PositionSnapshot:
    return PositionSnapshot(
        symbol="TEST",
        qty=7,
        avg_cost=Decimal(str(cost)),
        opened_at=bars.index[-30].to_pydatetime(),
        strategy="momentum_52w_high",
    )


def test_exit_stop_loss_on_close_at_or_below_85pct_of_cost(strategy):
    bars = _make_bars(_smooth_climb())
    last = float(bars["close"].iloc[-1])
    at_stop = strategy.exit_rules(_position(bars, last / 0.85), bars, as_of=_as_of(bars))
    assert at_stop is not None
    assert at_stop.reason == "stop_loss"
    assert at_stop.qty == 7

    just_above = strategy.exit_rules(
        _position(bars, last / 0.85 - 0.01), bars, as_of=_as_of(bars)
    )
    assert just_above is None


def test_exit_below_sma100(strategy):
    climb = _smooth_climb()
    closes = list(climb[:-10]) + list(climb[-11] * np.linspace(0.99, 0.90, 10))
    bars = _make_bars(closes)
    price = bars["adj_close"]
    assert price.iloc[-1] < sma(price, 100).iloc[-1]
    assert price.iloc[-1] >= 0.85 * price.iloc[-252:].max()  # not the near-high exit
    out = strategy.exit_rules(_position(bars, 50.0), bars, as_of=_as_of(bars))
    assert out is not None
    assert out.reason == "below_sma100"


def test_exit_left_near_high_set(strategy):
    # One old spike sets the 252-day high; price then sits flat above its
    # 100-day average but more than 15% below that high.
    closes = [100.0] * 60 + [130.0] + [100.0 + 0.01 * i for i in range(237)]
    bars = _make_bars(closes)
    price = bars["adj_close"]
    assert price.iloc[-1] >= sma(price, 100).iloc[-1]
    assert price.iloc[-1] < 0.85 * price.iloc[-252:].max()
    out = strategy.exit_rules(_position(bars, 50.0), bars, as_of=_as_of(bars))
    assert out is not None
    assert out.reason == "left_near_high_set"


def test_exit_hold_otherwise(strategy):
    bars = _make_bars(_smooth_climb())
    assert strategy.exit_rules(_position(bars, 50.0), bars, as_of=_as_of(bars)) is None


def test_exit_rules_need_a_year_of_bars(strategy):
    bars = _make_bars([100.0] * 200 + [10.0])  # would be a stop if evaluated
    assert strategy.exit_rules(_position(bars, 100.0), bars, as_of=_as_of(bars)) is None


def test_exit_rules_no_lookahead(strategy):
    closes = list(_smooth_climb()) + [1.0] * 5  # collapse after as_of
    bars = _make_bars(closes)
    as_of = bars.index[N_WED - 1].date()
    assert strategy.exit_rules(_position(bars, 50.0), bars, as_of=as_of) is None
    later = strategy.exit_rules(_position(bars, 50.0), bars, as_of=_as_of(bars))
    assert later is not None and later.reason == "stop_loss"


# ---------------------------------------------------------------------
# Slope quality
# ---------------------------------------------------------------------
def _series(log_returns: float, n: int = 90) -> pd.Series:
    return pd.Series(100.0 * np.exp(np.linspace(0.0, log_returns, n)))


def test_slope_quality_flat_is_half():
    assert Momentum52WeekHigh._slope_quality(pd.Series([100.0] * 90)) == pytest.approx(0.5)


def test_slope_quality_monotonic_in_slope():
    qualities = [Momentum52WeekHigh._slope_quality(_series(r)) for r in (-0.3, -0.1, 0.0, 0.1, 0.3, 0.6)]
    assert qualities == sorted(qualities)
    assert qualities[2] == pytest.approx(0.5)
    assert qualities[0] < 0.5 < qualities[-1]
    assert all(0.0 < q < 1.0 for q in qualities)


def test_slope_quality_penalises_jagged_climbs():
    smooth = _series(0.2)
    rng = np.random.default_rng(7)
    jagged = smooth * np.exp(rng.normal(0.0, 0.05, len(smooth)))
    assert Momentum52WeekHigh._slope_quality(jagged) < Momentum52WeekHigh._slope_quality(smooth)
