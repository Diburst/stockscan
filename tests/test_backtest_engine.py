"""Backtest engine end-to-end tests.

An in-memory ``bars_loader`` stands in for the database, and two tiny
strategies (one stop-based, one fixed-fraction) emit signals on chosen
dates so every fill, share count and exit reason below is deterministic.
SPY bars from the same loader drive the regime controls exactly as they
would in a real run; ``get_macro_series`` and ``sector_map`` are patched
on the engine module.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import ClassVar
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from stockscan.backtest import (
    BacktestConfig,
    BacktestEngine,
    FixedBpsSlippage,
    NoSlippage,
)
from stockscan.backtest import engine as engine_mod
from stockscan.strategies import ExitDecision, RawSignal, Strategy

N_BARS = 500
INDEX = pd.date_range("2022-01-03 21:00", periods=N_BARS, freq="B", tz="UTC")
DATES = [ts.date() for ts in INDEX]
START = DATES[320]
END = DATES[-1]
SIGNAL_DAY = DATES[330]


# -----------------------------------------------------------------------
# Synthetic bars
# -----------------------------------------------------------------------


def _frame(
    close: np.ndarray,
    *,
    symbol: str = "TEST",
    low: np.ndarray | None = None,
    volume: float = 5_000_000,
) -> pd.DataFrame:
    # Same columns ``get_bars`` returns, including the ``symbol`` column.
    return pd.DataFrame(
        {
            "symbol": symbol,
            "open": close,
            "high": close * 1.01,
            "low": close * 0.99 if low is None else low,
            "close": close,
            "adj_close": close,
            "volume": np.full(len(close), volume),
        },
        index=INDEX,
    )


def _symbol_of(bars: pd.DataFrame) -> str:
    """The live runner tags ``bars.attrs["symbol"]``; the ``symbol`` column
    is the fallback the DB frame always carries."""
    return str(bars.attrs.get("symbol") or bars["symbol"].iloc[-1])


def _flat_stock(symbol: str = "TEST", price: float = 100.0, *, breach_low_on: date | None = None) -> pd.DataFrame:
    close = np.full(N_BARS, price)
    low = close * 0.99
    if breach_low_on is not None:
        low = low.copy()
        low[DATES.index(breach_low_on)] = price * 0.5  # far below any stop
    return _frame(close, symbol=symbol, low=low)


def _spy(kind: str) -> pd.DataFrame:
    """``up``: gate open, calm. ``down``: gate closed. ``burst``: gate open,
    ±3% alternating closes from bar 300 on (top-tercile vol)."""
    if kind == "down":
        close = np.linspace(600.0, 300.0, N_BARS)
    else:
        close = np.linspace(300.0, 600.0, N_BARS)
        if kind == "burst":
            level = close[300]
            close[300:] = [level * (1.03 if i % 2 else 1.0) for i in range(N_BARS - 300)]
    return _frame(close, symbol="SPY")


def _loader(frames: dict[str, pd.DataFrame]):
    def load(symbol: str, start, end) -> pd.DataFrame:
        df = frames.get(symbol)
        if df is None:
            return pd.DataFrame()
        s = pd.Timestamp(start, tz="UTC")
        e = pd.Timestamp(end, tz="UTC") + pd.Timedelta(days=1)
        return df[(df.index >= s) & (df.index <= e)]

    return load


# -----------------------------------------------------------------------
# Strategies
# -----------------------------------------------------------------------


class _StopMomentum(Strategy):
    """Risks 1% against a stop 10% below entry on the dates in ``signal_days``.
    Exits only when ``exit_day`` says so — never on the stop."""

    name = "_bt_stop_momentum"
    version = "0.0.1"
    display_name = "Backtest fake (stop-based)"
    default_risk_pct = 0.01
    position_pct = None
    sizes_down_in_high_vol = True

    signal_days: ClassVar[frozenset[date]] = frozenset({SIGNAL_DAY})
    scores: ClassVar[dict[str, float]] = {}
    exit_day: ClassVar[date | None] = None

    def required_history(self) -> int:
        return 30

    def signals(self, bars, as_of):
        if as_of not in self.signal_days:
            return []
        symbol = _symbol_of(bars)
        entry = Decimal(str(float(bars["close"].iloc[-1])))
        return [
            RawSignal(
                strategy_name=self.name,
                strategy_version=self.version,
                symbol=symbol,
                side="long",
                score=Decimal(str(self.scores.get(symbol, 1.0))),
                suggested_entry=entry,
                suggested_stop=entry * Decimal("0.9"),
                metadata={"score_src": "test"},
            )
        ]

    def exit_rules(self, position, bars, as_of):
        if self.exit_day is not None and as_of >= self.exit_day:
            return ExitDecision(reason="strategy_exit", qty=position.qty)
        return None


class _FixedFractionMeanRev(_StopMomentum):
    """10% of equity per slot, no stop, ignores the vol scalar."""

    name = "_bt_fixed_fraction"
    display_name = "Backtest fake (fixed fraction)"
    default_risk_pct = 0.01
    position_pct = 0.10
    sizes_down_in_high_vol = False

    def signals(self, bars, as_of):
        out = []
        for sig in super().signals(bars, as_of):
            out.append(
                RawSignal(
                    strategy_name=self.name,
                    strategy_version=self.version,
                    symbol=sig.symbol,
                    side=sig.side,
                    score=sig.score,
                    suggested_entry=sig.suggested_entry,
                    suggested_stop=None,
                    metadata=sig.metadata,
                )
            )
        return out


class _NoVolScalingMomentum(_StopMomentum):
    name = "_bt_stop_no_vol_scaling"
    display_name = "Backtest fake (stop-based, opts out of vol scalar)"
    sizes_down_in_high_vol = False


@pytest.fixture(autouse=True)
def _reset_fakes():
    for cls in (_StopMomentum, _FixedFractionMeanRev, _NoVolScalingMomentum):
        cls.signal_days = frozenset({SIGNAL_DAY})
        cls.scores = {}
        cls.exit_day = None
    yield


@pytest.fixture(autouse=True)
def _no_db():
    with (
        patch.object(engine_mod, "get_macro_series", return_value=pd.Series(dtype=float)),
        patch.object(engine_mod, "sector_map", return_value={}),
        patch.object(engine_mod, "members_as_of", side_effect=AssertionError("universe must be explicit")),
    ):
        yield


def _config(strategy_cls=_StopMomentum, universe=("TEST",), **kw) -> BacktestConfig:
    args = dict(
        strategy_cls=strategy_cls,
        start_date=START,
        end_date=END,
        starting_capital=Decimal("100000"),
        slippage=NoSlippage(),
        universe=list(universe),
        max_positions=15,
        # Wide enough that the 1%-risk / 10%-stop fake's $10k slot is not
        # clipped; individual tests tighten it on purpose.
        max_position_pct=Decimal("0.15"),
    )
    args.update(kw)
    return BacktestConfig(**args)


def _run(frames: dict[str, pd.DataFrame], **kw):
    return BacktestEngine(_config(**kw), bars_loader=_loader(frames)).run()


def _next_day(d: date) -> date:
    return DATES[DATES.index(d) + 1]


# -----------------------------------------------------------------------
# Fills, exits, equity curve
# -----------------------------------------------------------------------


class TestFills:
    def test_entry_fills_next_open_and_sizes_by_stop(self):
        result = _run({"TEST": _flat_stock(), "SPY": _spy("up")})
        assert len(result.trades) == 1
        t = result.trades[0]
        assert t.symbol == "TEST"
        assert t.entry_date == _next_day(SIGNAL_DAY)
        assert t.entry_price == Decimal("100")
        assert t.qty == 100  # $100k × 1% / $10 stop distance
        assert t.entry_stop == Decimal("90")
        assert t.entry_metadata == {"score_src": "test"}

    def test_no_signal_on_last_day_is_never_queued(self):
        _StopMomentum.signal_days = frozenset({END})
        result = _run({"TEST": _flat_stock(), "SPY": _spy("up")})
        assert result.trades == []

    def test_strategy_exit_fills_next_open(self):
        _StopMomentum.exit_day = DATES[340]
        result = _run({"TEST": _flat_stock(), "SPY": _spy("up")})
        assert len(result.trades) == 1
        t = result.trades[0]
        assert t.exit_reason == "strategy_exit"
        assert t.exit_date == _next_day(DATES[340])

    def test_open_position_is_force_closed_at_end(self):
        result = _run({"TEST": _flat_stock(), "SPY": _spy("up")})
        t = result.trades[0]
        assert t.exit_reason == "end_of_backtest"
        assert t.exit_date == END

    def test_engine_applies_no_stop_of_its_own(self):
        # The low breaches the strategy's stop three days after entry, but
        # the strategy's exit_rules never fires → the position rides to the
        # end. Stops are the strategy's decision alone.
        breach = DATES[334]
        result = _run({"TEST": _flat_stock(breach_low_on=breach), "SPY": _spy("up")})
        assert len(result.trades) == 1
        t = result.trades[0]
        assert t.exit_reason == "end_of_backtest"
        assert t.exit_date == END
        assert t.exit_date > breach

    def test_equity_curve_one_point_per_trading_day(self):
        result = _run({"TEST": _flat_stock(), "SPY": _spy("up")})
        assert len(result.equity_curve) == len([d for d in DATES if START <= d <= END])
        assert float(result.equity_curve.iloc[0]) == pytest.approx(100000.0)
        assert result.equity_curve.index[0] == pd.Timestamp(START)
        assert result.report.num_trades == len(result.trades)

    def test_flat_price_leaves_equity_flat(self):
        result = _run({"TEST": _flat_stock(), "SPY": _spy("up")})
        assert float(result.equity_curve.iloc[-1]) == pytest.approx(100000.0)
        assert float(result.positions_value.max()) == pytest.approx(10000.0)

    def test_slippage_hurts_returns(self):
        frames = {"TEST": _flat_stock(), "SPY": _spy("up")}
        clean = _run(frames)
        slipped = _run(frames, slippage=FixedBpsSlippage(bps=Decimal("20")))
        assert slipped.trades[0].entry_price == Decimal("100.2")
        assert slipped.report.total_return_pct < clean.report.total_return_pct

    def test_commission_charged_both_ways(self):
        _StopMomentum.exit_day = DATES[340]
        result = _run({"TEST": _flat_stock(), "SPY": _spy("up")}, commission_per_trade=Decimal("1"))
        assert result.trades[0].commission == Decimal("2")
        assert float(result.equity_curve.iloc[-1]) == pytest.approx(99998.0)

    def test_no_trading_days_raises(self):
        with pytest.raises(ValueError):
            BacktestEngine(_config(), bars_loader=_loader({})).run()


class TestNoLookahead:
    def test_overlapping_windows_agree_on_the_overlap(self):
        _StopMomentum.signal_days = frozenset({DATES[330], DATES[380], DATES[430]})
        _StopMomentum.exit_day = None
        frames = {"TEST": _flat_stock(), "SPY": _spy("up")}

        class _Rolling(_StopMomentum):
            name = "_bt_rolling_exit"
            display_name = "x"

            def exit_rules(self, position, bars, as_of):
                if (as_of - position.opened_at.date()).days >= 20:
                    return ExitDecision(reason="time", qty=position.qty)
                return None

        mid = DATES[420]
        short = _run(frames, strategy_cls=_Rolling, end_date=mid)
        long = _run(frames, strategy_cls=_Rolling)
        short_closed = [t for t in short.trades if t.exit_date < mid]
        long_closed = [t for t in long.trades if t.exit_date < mid]
        assert len(short_closed) == 2
        assert [(t.entry_date, t.exit_date, t.entry_price, t.exit_price) for t in short_closed] == [
            (t.entry_date, t.exit_date, t.entry_price, t.exit_price) for t in long_closed
        ]


# -----------------------------------------------------------------------
# Regime controls
# -----------------------------------------------------------------------


class TestRegime:
    def test_gate_closed_blocks_every_entry(self):
        result = _run({"TEST": _flat_stock(), "SPY": _spy("down")})
        assert result.trades == []
        assert float(result.equity_curve.iloc[-1]) == pytest.approx(100000.0)

    def test_gate_open_entries_fill_next_open(self):
        result = _run({"TEST": _flat_stock(), "SPY": _spy("up")})
        assert len(result.trades) == 1
        assert result.trades[0].entry_date == _next_day(SIGNAL_DAY)

    def test_missing_spy_is_neutral(self):
        result = _run({"TEST": _flat_stock()})
        assert len(result.trades) == 1
        assert result.trades[0].qty == 100

    def test_vol_scalar_shrinks_sizes_down_strategy(self):
        result = _run({"TEST": _flat_stock(), "SPY": _spy("burst")})
        assert len(result.trades) == 1
        assert result.trades[0].qty == 50  # floor 0.5 × 100

    def test_vol_scalar_ignored_when_strategy_opts_out(self):
        result = _run({"TEST": _flat_stock(), "SPY": _spy("burst")}, strategy_cls=_NoVolScalingMomentum)
        assert result.trades[0].qty == 100

    def test_regime_frame_loaded_once_per_run(self):
        calls: list[str] = []
        base = _loader({"TEST": _flat_stock(), "SPY": _spy("up")})

        def counting(symbol, start, end):
            calls.append(symbol)
            return base(symbol, start, end)

        _StopMomentum.signal_days = frozenset({DATES[330], DATES[400]})
        BacktestEngine(_config(), bars_loader=counting).run()
        assert calls.count("SPY") == 1

    def test_credit_stress_blocks_entries(self):
        oas_idx = pd.DatetimeIndex(DATES)
        values = np.full(N_BARS, 3.5)
        values[325:] = np.linspace(4.0, 7.0, N_BARS - 325)  # stress from just before the signal
        oas = pd.Series(values, index=oas_idx)
        with patch.object(engine_mod, "get_macro_series", return_value=oas):
            result = _run({"TEST": _flat_stock(), "SPY": _spy("up")})
        assert result.trades == []


# -----------------------------------------------------------------------
# Sizing rules and caps
# -----------------------------------------------------------------------


class TestSizingAndCaps:
    def test_fixed_fraction_strategy_with_no_stop_fills(self):
        result = _run({"TEST": _flat_stock(), "SPY": _spy("up")}, strategy_cls=_FixedFractionMeanRev)
        assert len(result.trades) == 1
        t = result.trades[0]
        assert t.qty == 100  # $10k / $100
        assert t.entry_stop is None

    def test_max_position_pct_caps_qty(self):
        result = _run({"TEST": _flat_stock(), "SPY": _spy("up")}, max_position_pct=Decimal("0.05"))
        assert result.trades[0].qty == 50

    def test_sector_cap_binds_within_the_day_best_score_first(self):
        _StopMomentum.scores = {"A": 3.0, "B": 2.0, "C": 1.0}
        frames = {s: _flat_stock(s) for s in ("A", "B", "C")}
        frames["SPY"] = _spy("up")
        with patch.object(engine_mod, "sector_map", return_value={"A": "Tech", "B": "Tech", "C": "Tech"}):
            result = _run(frames, universe=("A", "B", "C"), max_sector_pct=Decimal("0.25"))
        # $10k each against a $25k Tech cap → the two best scores fill.
        assert sorted(t.symbol for t in result.trades) == ["A", "B"]

    def test_max_positions_binds(self):
        _StopMomentum.scores = {"A": 1.0, "B": 9.0, "C": 5.0}
        frames = {s: _flat_stock(s) for s in ("A", "B", "C")}
        frames["SPY"] = _spy("up")
        result = _run(frames, universe=("A", "B", "C"), max_positions=2)
        assert sorted(t.symbol for t in result.trades) == ["B", "C"]

    def test_strategy_max_open_positions_binds(self):
        class _Capped(_StopMomentum):
            name = "_bt_capped"
            display_name = "x"
            max_open_positions = 1

        _StopMomentum.scores = {"A": 2.0, "B": 1.0}
        frames = {"A": _flat_stock("A"), "B": _flat_stock("B"), "SPY": _spy("up")}
        result = _run(frames, strategy_cls=_Capped, universe=("A", "B"))
        assert [t.symbol for t in result.trades] == ["A"]

    def test_adv_cap_binds_on_thin_names(self):
        thin = _frame(np.full(N_BARS, 100.0), volume=1000)  # ADV $100k → 5% = $5k
        result = _run({"TEST": thin, "SPY": _spy("up")})
        assert result.trades == []

    def test_insufficient_cash_drops_the_order(self):
        class _Greedy(_FixedFractionMeanRev):
            name = "_bt_greedy"
            display_name = "x"
            position_pct = 0.60

        _StopMomentum.scores = {"A": 2.0, "B": 1.0}
        frames = {"A": _flat_stock("A"), "B": _flat_stock("B"), "SPY": _spy("up")}
        result = _run(
            frames,
            strategy_cls=_Greedy,
            universe=("A", "B"),
            starting_capital=Decimal("1000"),
            max_position_pct=Decimal("1"),
        )
        # Both size to $600 against $1k equity; A fills, B's fill is refused
        # for lack of cash and the order lapses rather than going negative.
        assert [t.symbol for t in result.trades] == ["A"]
        assert result.trades[0].qty == 6
        assert float(result.equity_curve.min()) == pytest.approx(1000.0)

    def test_already_held_symbol_is_not_re_entered(self):
        _StopMomentum.signal_days = frozenset({DATES[330], DATES[335]})
        result = _run({"TEST": _flat_stock(), "SPY": _spy("up")})
        assert len(result.trades) == 1


def test_config_has_no_engine_level_risk_knobs():
    fields = set(BacktestConfig.__dataclass_fields__)
    assert "risk_pct" not in fields
    assert "params" not in fields
    assert {"max_position_pct", "max_sector_pct", "max_adv_pct", "max_drawdown"} <= fields


def test_engine_tags_bars_with_symbol_like_the_live_runner():
    class _AttrsOnly(_StopMomentum):
        name = "_bt_attrs_only"
        display_name = "x"

        def signals(self, bars, as_of):
            if as_of not in self.signal_days or "symbol" not in bars.attrs:
                return []
            return super().signals(bars, as_of)

    result = _run({"TEST": _flat_stock(), "SPY": _spy("up")}, strategy_cls=_AttrsOnly)
    assert len(result.trades) == 1


def test_bars_never_read_past_as_of():
    engine = BacktestEngine(_config(), bars_loader=_loader({"TEST": _flat_stock()}))
    view = engine._bars("TEST", DATES[100])
    assert view.index[-1].date() == DATES[100]
    assert len(view) == 101
    earlier = engine._bars("TEST", DATES[100] - timedelta(days=1))
    assert len(earlier) < 101
