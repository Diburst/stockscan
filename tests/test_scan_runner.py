"""``ScanRunner._run_in_session`` — the live scanner's one pass, end to end.

No database: a fake ``Session`` answers each SQL statement by substring
and records the ``signals`` rows the runner persists. ``get_bars``,
``detect_regime``, ``sector_map``, ``ensure_strategy_version`` and the
settings object are patched on ``stockscan.scan.runner``. The strategy is
a minimal ``Strategy`` subclass built per test (the conftest registry
fixture wipes it afterwards), so every rejection reason, share count and
cap below is a consequence of the runner's own logic.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from stockscan.regime.store import MarketRegime
from stockscan.scan import runner as runner_mod
from stockscan.scan.runner import ScanRunner, ScanSummary, avg_dollar_volume
from stockscan.strategies import RawSignal, Strategy

AS_OF = date(2026, 4, 28)
EQUITY = Decimal("100000")


# -----------------------------------------------------------------------
# Fakes
# -----------------------------------------------------------------------


class _Result:
    def __init__(self, rows=None, one=None):
        self._rows = rows or []
        self._one = one

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return list(self._rows)

    def one(self):
        return self._one


class _FakeSession:
    """Answers the runner's SQL by substring; records persisted signals."""

    def __init__(self, *, equity_row=None, positions=None, earnings=None):
        self.equity_row = equity_row
        self.positions = positions or []
        self.earnings = earnings or []
        self.signal_rows: list[dict] = []
        self.run_rows: list[dict] = []

    def execute(self, sql, params=None):
        text = str(sql)
        if "FROM equity_history" in text:
            return _Result(rows=[self.equity_row] if self.equity_row else [])
        if "FROM positions" in text:
            return _Result(rows=self.positions)
        if "FROM earnings_calendar" in text:
            return _Result(rows=self.earnings)
        if "INSERT INTO strategy_runs" in text:
            self.run_rows.append(dict(params))
            return _Result(one=SimpleNamespace(run_id=41))
        if "INSERT INTO signals" in text:
            self.signal_rows.append(dict(params))
            return _Result()
        raise AssertionError(f"unexpected SQL: {text[:80]}")

    # Convenience views -------------------------------------------------
    def by_symbol(self) -> dict[str, dict]:
        return {r["symbol"]: r for r in self.signal_rows}

    def passing(self) -> dict[str, dict]:
        return {s: r for s, r in self.by_symbol().items() if r["status"] == "new"}

    def rejected(self) -> dict[str, str]:
        return {s: r["reason"] for s, r in self.by_symbol().items() if r["status"] == "rejected"}


def _make_strategy(
    *,
    emit: dict[str, dict],
    position_pct: float | None = None,
    sizes_down: bool = True,
    risk_pct: float = 0.01,
    max_open: int | None = None,
) -> type[Strategy]:
    """A strategy that emits one long per symbol in ``emit``:
    ``{symbol: {"entry": .., "stop": .., "score": ..}}``."""

    class _ScanStrategy(Strategy):
        name = "_scan_runner_fake"
        version = "0.0.1"
        display_name = "Scan runner fake"
        default_risk_pct = risk_pct
        position_pct = None
        sizes_down_in_high_vol = sizes_down
        max_open_positions = max_open
        _emit: ClassVar[dict[str, dict]] = emit

        def required_history(self) -> int:
            return 25

        def signals(self, bars, as_of):
            symbol = bars.attrs["symbol"]
            spec = self._emit.get(symbol)
            if spec is None:
                return []
            stop = spec.get("stop", "90")
            return [
                RawSignal(
                    strategy_name=self.name,
                    strategy_version=self.version,
                    symbol=symbol,
                    side=spec.get("side", "long"),
                    score=Decimal(str(spec.get("score", 1))),
                    suggested_entry=Decimal(str(spec.get("entry", "100"))),
                    suggested_stop=Decimal(str(stop)) if stop is not None else None,
                    metadata={"why": "test"},
                )
            ]

        def exit_rules(self, position, bars, as_of):
            return None

    _ScanStrategy.position_pct = position_pct
    return _ScanStrategy


def _bars(n: int = 30, *, close: float = 100.0, volume: float = 5_000_000) -> pd.DataFrame:
    idx = pd.date_range(end="2026-04-28 21:00", periods=n, freq="B", tz="UTC")
    c = np.full(n, close)
    return pd.DataFrame(
        {"open": c, "high": c, "low": c, "close": c, "adj_close": c, "volume": np.full(n, volume)},
        index=idx,
    )


def _regime(*, gate_open=True, stress=False, vol_scalar="1.0") -> MarketRegime:
    return MarketRegime(
        as_of_date=AS_OF,
        regime="credit_stress" if stress else ("risk_on" if gate_open else "risk_off"),
        trend_gate_open=gate_open,
        days_on_side=12,
        spy_close=Decimal("520"),
        spy_sma200=Decimal("480"),
        spy_sma200_slope_20d=Decimal("0.01"),
        realized_vol_20d=Decimal("0.2"),
        realized_vol_pct_rank=Decimal("0.5"),
        vol_scalar=Decimal(vol_scalar) if vol_scalar is not None else None,
        hy_oas_level=Decimal("3.5"),
        hy_oas_pct_rank=Decimal("0.4"),
        credit_stress_flag=stress,
    )


def _settings(**overrides) -> SimpleNamespace:
    base = dict(
        starting_equity=Decimal("100000"),
        max_positions=15,
        max_position_pct=Decimal("0.15"),
        max_sector_pct=Decimal("0.25"),
        max_adv_pct=Decimal("0.05"),
        drawdown_circuit_breaker=Decimal("0.15"),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _run(
    strategy_cls,
    symbols,
    *,
    session=None,
    regime=None,
    bars_for=None,
    sectors=None,
    settings=None,
) -> tuple[ScanSummary, _FakeSession]:
    session = session or _FakeSession(
        equity_row=SimpleNamespace(total_equity=EQUITY, high_water_mark=EQUITY)
    )
    bars_for = bars_for or {}

    def fake_get_bars(symbol, start, end, *, session=None):
        return bars_for.get(symbol, _bars())

    with (
        patch.object(runner_mod, "get_bars", side_effect=fake_get_bars),
        patch.object(runner_mod, "detect_regime", return_value=regime),
        patch.object(runner_mod, "sector_map", return_value=sectors or {}),
        patch.object(runner_mod, "ensure_strategy_version") as ensure,
        patch.object(runner_mod, "members_as_of", return_value=[]),
        patch.object(runner_mod, "settings", settings or _settings()),
    ):
        summary = ScanRunner(session=session)._run_in_session(session, strategy_cls, AS_OF, symbols)
    ensure.assert_called_once_with(strategy_cls, session=session)
    return summary, session


# -----------------------------------------------------------------------
# Happy path + summary shape
# -----------------------------------------------------------------------


class TestPassingSignal:
    def test_stop_based_signal_is_sized_and_persisted(self):
        cls = _make_strategy(emit={"AAPL": {"entry": "100", "stop": "90"}})
        summary, s = _run(cls, ["AAPL", "MSFT"], regime=_regime())

        assert summary.run_id == 41
        assert summary.strategy_name == cls.name
        assert summary.strategy_version == cls.version
        assert summary.as_of_date == AS_OF
        assert summary.universe_size == 2
        assert summary.signals_emitted == 1
        assert summary.rejected_count == 0
        assert summary.regime_label == "risk_on"
        assert summary.vol_scalar == 1.0

        row = s.passing()["AAPL"]
        assert row["qty"] == 100  # $100k × 1% / $10
        assert row["stop"] == Decimal("90")
        assert row["status"] == "new"
        assert row["reason"] is None
        assert row["run_id"] == 41
        assert s.run_rows[0]["s"] == 1 and s.run_rows[0]["r"] == 0

    def test_symbol_with_short_history_is_skipped_silently(self):
        cls = _make_strategy(emit={"AAPL": {}, "NEW": {}})
        summary, s = _run(cls, ["AAPL", "NEW"], regime=_regime(), bars_for={"NEW": _bars(10)})
        assert set(s.by_symbol()) == {"AAPL"}
        assert summary.universe_size == 2

    def test_signals_exception_skips_symbol_only(self):
        cls = _make_strategy(emit={"AAPL": {}, "BAD": {}})

        original = cls.signals

        def boom(self, bars, as_of):
            if bars.attrs["symbol"] == "BAD":
                raise RuntimeError("indicator blew up")
            return original(self, bars, as_of)

        cls.signals = boom
        summary, s = _run(cls, ["BAD", "AAPL"], regime=_regime())
        assert set(s.passing()) == {"AAPL"}
        assert summary.signals_emitted == 1


# -----------------------------------------------------------------------
# Regime: entry block
# -----------------------------------------------------------------------


class TestRegimeBlock:
    def test_trend_gate_closed_rejects_every_long_without_sizing(self):
        cls = _make_strategy(emit={"AAPL": {}, "MSFT": {}})
        with patch.object(runner_mod, "size_for_strategy") as sizer:
            summary, s = _run(cls, ["AAPL", "MSFT"], regime=_regime(gate_open=False))
        sizer.assert_not_called()
        assert summary.signals_emitted == 0
        assert summary.rejected_count == 2
        assert summary.regime_label == "risk_off"
        assert s.rejected() == {"AAPL": "trend_gate_closed", "MSFT": "trend_gate_closed"}
        assert all(r["qty"] == 0 for r in s.signal_rows)

    def test_credit_stress_rejects_longs_even_with_gate_open(self):
        cls = _make_strategy(emit={"AAPL": {}})
        summary, s = _run(cls, ["AAPL"], regime=_regime(gate_open=True, stress=True))
        assert s.rejected() == {"AAPL": "credit_stress_long_block"}
        assert summary.regime_label == "credit_stress"

    def test_credit_stress_reason_wins_over_closed_gate(self):
        cls = _make_strategy(emit={"AAPL": {}})
        _, s = _run(cls, ["AAPL"], regime=_regime(gate_open=False, stress=True))
        assert s.rejected() == {"AAPL": "credit_stress_long_block"}

    def test_shorts_are_not_blocked_by_the_gate(self):
        cls = _make_strategy(emit={"AAPL": {"side": "short", "stop": "110"}})
        _, s = _run(cls, ["AAPL"], regime=_regime(gate_open=False))
        # The sizer is long-only and rejects the short's stop; the point is
        # that the regime block did not fire for a non-long side.
        assert s.rejected()["AAPL"] == "stop_above_entry"

    def test_missing_regime_sizes_neutrally_and_does_not_block(self):
        cls = _make_strategy(emit={"AAPL": {}})
        summary, s = _run(cls, ["AAPL"], regime=None)
        assert summary.regime_label is None
        assert summary.vol_scalar == 1.0
        assert s.passing()["AAPL"]["qty"] == 100

    def test_regime_detection_failure_is_neutral(self):
        cls = _make_strategy(emit={"AAPL": {}})
        session = _FakeSession(equity_row=SimpleNamespace(total_equity=EQUITY, high_water_mark=EQUITY))
        with (
            patch.object(runner_mod, "get_bars", return_value=_bars()),
            patch.object(runner_mod, "detect_regime", side_effect=RuntimeError("db down")),
            patch.object(runner_mod, "sector_map", return_value={}),
            patch.object(runner_mod, "ensure_strategy_version"),
            patch.object(runner_mod, "settings", _settings()),
        ):
            summary = ScanRunner(session=session)._run_in_session(session, cls, AS_OF, ["AAPL"])
        assert summary.regime_label is None
        assert session.passing()["AAPL"]["qty"] == 100


# -----------------------------------------------------------------------
# Regime: vol scalar
# -----------------------------------------------------------------------


class TestVolScalar:
    def test_shrinks_qty_for_sizes_down_strategy(self):
        cls = _make_strategy(emit={"AAPL": {}}, sizes_down=True)
        summary, s = _run(cls, ["AAPL"], regime=_regime(vol_scalar="0.5"))
        assert summary.vol_scalar == 0.5
        assert s.passing()["AAPL"]["qty"] == 50

    def test_leaves_qty_alone_when_strategy_opts_out(self):
        cls = _make_strategy(emit={"AAPL": {}}, sizes_down=False)
        summary, s = _run(cls, ["AAPL"], regime=_regime(vol_scalar="0.5"))
        assert summary.vol_scalar == 0.5  # reported, not applied
        assert s.passing()["AAPL"]["qty"] == 100

    def test_null_scalar_on_row_is_neutral(self):
        cls = _make_strategy(emit={"AAPL": {}})
        summary, s = _run(cls, ["AAPL"], regime=_regime(vol_scalar=None))
        assert summary.vol_scalar == 1.0
        assert s.passing()["AAPL"]["qty"] == 100

    def test_scalar_that_zeroes_the_size_is_a_rejection(self):
        cls = _make_strategy(emit={"AAPL": {"entry": "100", "stop": "90"}})
        session = _FakeSession(
            equity_row=SimpleNamespace(total_equity=Decimal("1000"), high_water_mark=Decimal("1000"))
        )
        _, s = _run(cls, ["AAPL"], session=session, regime=_regime(vol_scalar="0.5"))
        assert s.rejected() == {"AAPL": "vol_scalar_zero_size"}


# -----------------------------------------------------------------------
# Fixed-fraction strategies
# -----------------------------------------------------------------------


class TestFixedFraction:
    def test_sized_from_position_pct_with_no_stop(self):
        cls = _make_strategy(emit={"AAPL": {"entry": "50", "stop": None}}, position_pct=0.10)
        _, s = _run(cls, ["AAPL"], regime=_regime())
        row = s.passing()["AAPL"]
        assert row["qty"] == 200  # $10k / $50
        assert row["stop"] is None

    def test_stop_based_strategy_emitting_no_stop_is_rejected(self):
        cls = _make_strategy(emit={"AAPL": {"stop": None}}, position_pct=None)
        _, s = _run(cls, ["AAPL"], regime=_regime())
        assert s.rejected() == {"AAPL": "no_stop_and_no_position_pct"}

    def test_max_position_pct_caps_fraction(self):
        cls = _make_strategy(emit={"AAPL": {"entry": "100", "stop": None}}, position_pct=0.50)
        _, s = _run(cls, ["AAPL"], regime=_regime(), settings=_settings(max_position_pct=Decimal("0.08")))
        assert s.passing()["AAPL"]["qty"] == 80


# -----------------------------------------------------------------------
# Caps bind within one pass
# -----------------------------------------------------------------------


class TestCapsAccumulate:
    def test_sector_cap_binds_against_earlier_winners(self):
        # Each candidate is $10k; Tech cap is 25% of $100k = $25k → two fit.
        cls = _make_strategy(
            emit={
                "AAPL": {"score": 3},
                "MSFT": {"score": 2},
                "GOOG": {"score": 1},
                "XOM": {"score": 0.5},
            }
        )
        sectors = {"AAPL": "Tech", "MSFT": "Tech", "GOOG": "Tech", "XOM": "Energy"}
        summary, s = _run(cls, ["GOOG", "XOM", "MSFT", "AAPL"], regime=_regime(), sectors=sectors)
        assert set(s.passing()) == {"AAPL", "MSFT", "XOM"}
        assert s.rejected() == {"GOOG": "sector_Tech_would_exceed_25%_of_equity"}
        assert summary.signals_emitted == 3
        assert summary.rejected_count == 1

    def test_max_positions_binds_within_the_pass_best_score_first(self):
        cls = _make_strategy(emit={"A": {"score": 1}, "B": {"score": 9}, "C": {"score": 5}})
        summary, s = _run(cls, ["A", "B", "C"], regime=_regime(), settings=_settings(max_positions=2))
        assert set(s.passing()) == {"B", "C"}
        assert s.rejected() == {"A": "max_concurrent_positions_2"}

    def test_strategy_max_open_positions_counts_existing_book(self):
        cls = _make_strategy(emit={"A": {"score": 2}, "B": {"score": 1}}, max_open=2)
        session = _FakeSession(
            equity_row=SimpleNamespace(total_equity=EQUITY, high_water_mark=EQUITY),
            positions=[SimpleNamespace(symbol="Z", strategy=cls.name, qty=10, avg_cost=Decimal("50"))],
        )
        _, s = _run(cls, ["A", "B"], session=session, regime=_regime())
        assert set(s.passing()) == {"A"}
        assert s.rejected() == {"B": f"max_{cls.name}_positions_2"}

    def test_existing_position_rejected_as_already_held(self):
        cls = _make_strategy(emit={"AAPL": {}})
        session = _FakeSession(
            equity_row=SimpleNamespace(total_equity=EQUITY, high_water_mark=EQUITY),
            positions=[SimpleNamespace(symbol="AAPL", strategy="other", qty=10, avg_cost=Decimal("50"))],
        )
        _, s = _run(cls, ["AAPL"], session=session, regime=_regime())
        assert s.rejected() == {"AAPL": "already_in_position_via_other"}

    def test_existing_sector_exposure_counts_toward_cap(self):
        cls = _make_strategy(emit={"AAPL": {}})
        # $20k already in Tech + $10k candidate > $25k cap.
        session = _FakeSession(
            equity_row=SimpleNamespace(total_equity=EQUITY, high_water_mark=EQUITY),
            positions=[SimpleNamespace(symbol="MSFT", strategy="x", qty=200, avg_cost=Decimal("100"))],
        )
        _, s = _run(
            cls, ["AAPL"], session=session, regime=_regime(), sectors={"AAPL": "Tech", "MSFT": "Tech"}
        )
        assert s.rejected() == {"AAPL": "sector_Tech_would_exceed_25%_of_equity"}

    def test_earnings_within_window_rejected(self):
        cls = _make_strategy(emit={"AAPL": {}})
        session = _FakeSession(
            equity_row=SimpleNamespace(total_equity=EQUITY, high_water_mark=EQUITY),
            earnings=[SimpleNamespace(symbol="AAPL")],
        )
        _, s = _run(cls, ["AAPL"], session=session, regime=_regime())
        assert s.rejected() == {"AAPL": "earnings_within_5_trading_days"}


# -----------------------------------------------------------------------
# ADV
# -----------------------------------------------------------------------


class TestAdv:
    def test_avg_dollar_volume_helper(self):
        assert avg_dollar_volume(_bars(30, close=10.0, volume=1000)) == Decimal("10000.00")
        assert avg_dollar_volume(_bars(19)) is None

    def test_adv_cap_binds_on_thin_names(self):
        # ADV = $100 × 1000 = $100k; 5% cap = $5k; candidate is $10k.
        cls = _make_strategy(emit={"THIN": {}, "AAPL": {}})
        _, s = _run(cls, ["THIN", "AAPL"], regime=_regime(), bars_for={"THIN": _bars(volume=1000)})
        assert s.rejected() == {"THIN": "position_exceeds_5%_of_20d_adv"}
        assert "AAPL" in s.passing()


# -----------------------------------------------------------------------
# Equity source
# -----------------------------------------------------------------------


class TestEquity:
    def test_falls_back_to_starting_equity_when_history_empty(self):
        cls = _make_strategy(emit={"AAPL": {"entry": "100", "stop": "90"}})
        session = _FakeSession(equity_row=None)
        _, s = _run(
            cls, ["AAPL"], session=session, regime=_regime(),
            settings=_settings(starting_equity=Decimal("50000")),
        )
        assert s.passing()["AAPL"]["qty"] == 50  # $50k × 1% / $10

    def test_uses_latest_equity_history_row(self):
        cls = _make_strategy(emit={"AAPL": {"entry": "100", "stop": "90"}})
        session = _FakeSession(
            equity_row=SimpleNamespace(total_equity=Decimal("250000"), high_water_mark=Decimal("250000"))
        )
        _, s = _run(cls, ["AAPL"], session=session, regime=_regime())
        assert s.passing()["AAPL"]["qty"] == 250

    def test_drawdown_breaker_rejects(self):
        cls = _make_strategy(emit={"AAPL": {}})
        session = _FakeSession(
            equity_row=SimpleNamespace(total_equity=Decimal("80000"), high_water_mark=Decimal("100000"))
        )
        _, s = _run(cls, ["AAPL"], session=session, regime=_regime())
        assert s.rejected()["AAPL"].startswith("drawdown_circuit_breaker_at_20.0%")


# -----------------------------------------------------------------------
# run() resolves the strategy by name
# -----------------------------------------------------------------------


def test_run_unknown_strategy_raises():
    with pytest.raises(KeyError):
        ScanRunner(session=_FakeSession()).run("_no_such_strategy", AS_OF, symbols=[])
