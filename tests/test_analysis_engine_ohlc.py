"""Engine contract for what is built straight off the bars frame: the
ohlc_history slice behind the /analysis candlestick charts and the
per-symbol scalars the options proposal engine reads (20-day ADV, the
1-day move raw and net of sector, one day of vol).

No DB: bars are passed directly, the session is mocked and the sector leg
is patched. The sub-modules that touch the session (options_context)
soft-fail and don't affect any of these.
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from stockscan.analysis import engine as engine_mod
from stockscan.analysis.engine import _CHART_HISTORY_DAYS, analyze_symbol


def _bars(n: int) -> pd.DataFrame:
    idx = pd.date_range("2022-01-03", periods=n, freq="B")
    rng = np.random.default_rng(11)
    closes = 100 + np.cumsum(rng.normal(0, 1, n))
    return pd.DataFrame(
        {
            "open": closes + rng.normal(0, 0.3, n),
            "high": closes + 1.0,
            "low": closes - 1.0,
            "close": closes,
            "adj_close": closes,
            "volume": rng.integers(100_000, 1_000_000, n),
        },
        index=idx,
    )


def test_ohlc_history_shape():
    bars = _bars(300)
    a = analyze_symbol("TEST", bars=bars, session=MagicMock())
    assert a.available
    assert a.ohlc_history, "ohlc_history should be populated"
    rec = a.ohlc_history[-1]
    for k in ("time", "open", "high", "low", "close", "volume"):
        assert k in rec, f"ohlc record missing {k}"
    # Lightweight-Charts time is the YYYY-MM-DD string.
    assert isinstance(rec["time"], str) and rec["time"][4] == "-"
    # Chronological: last record is the most recent close.
    assert rec["close"] == float(bars["close"].iloc[-1])


def test_ohlc_history_capped():
    n = _CHART_HISTORY_DAYS + 150
    a = analyze_symbol("TEST", bars=_bars(n), session=MagicMock())
    assert len(a.ohlc_history) == _CHART_HISTORY_DAYS
    # closes_history shares the same cap.
    assert len(a.closes_history) == _CHART_HISTORY_DAYS


# ---------------------------------------------------------------------------
# Per-symbol scalars for the proposal engine
# ---------------------------------------------------------------------------
def test_adv_20d_is_the_mean_dollar_volume_of_the_last_20_bars():
    bars = _bars(300)
    a = analyze_symbol("TEST", bars=bars, session=MagicMock())
    tail = bars.iloc[-20:]
    assert a.adv_20d == pytest.approx(float((tail["close"] * tail["volume"]).mean()))
    assert a.last_volume == pytest.approx(float(bars["close"].iloc[-1] * bars["volume"].iloc[-1]))


def test_adv_20d_none_with_fewer_than_20_bars():
    a = analyze_symbol("TEST", bars=_bars(15), session=MagicMock())
    assert a.adv_20d is None


def test_day_move_is_the_adj_close_change_and_residual_nets_the_sector():
    bars = _bars(300)
    prev, last = float(bars["adj_close"].iloc[-2]), float(bars["adj_close"].iloc[-1])
    expected = (last - prev) / prev * 100.0

    def fake_residual(frame, as_of, *, lookback):
        assert frame.attrs["symbol"] == "TEST" and lookback == 1
        return expected / 100.0 - 0.012  # sector was up 1.2%

    with patch.object(engine_mod, "sector_relative_return", side_effect=fake_residual):
        a = analyze_symbol("TEST", bars=bars, as_of=date(2023, 3, 1), session=MagicMock())
    assert a.day_move_pct == pytest.approx(expected)
    assert a.day_move_residual_pct == pytest.approx(expected - 1.2)


def test_residual_is_none_without_a_sector_composite():
    with patch.object(engine_mod, "sector_relative_return", return_value=None):
        a = analyze_symbol("TEST", bars=_bars(300), session=MagicMock())
    assert a.day_move_pct is not None
    assert a.day_move_residual_pct is None


def test_residual_failure_is_soft():
    with patch.object(engine_mod, "sector_relative_return", side_effect=RuntimeError("no db")):
        a = analyze_symbol("TEST", bars=_bars(300), session=MagicMock())
    assert a.available
    assert a.day_move_residual_pct is None
    assert "sector" not in a.failures


def test_daily_sigma_is_the_annual_vol_over_root_252():
    a = analyze_symbol("TEST", bars=_bars(300), session=MagicMock())
    annual = a.volatility.ewma_vol_pct or a.volatility.realized_vol_21d_pct
    assert annual is not None
    assert a.daily_sigma_pct == pytest.approx(annual / math.sqrt(252))


def test_daily_sigma_none_with_too_few_bars_for_vol():
    a = analyze_symbol("TEST", bars=_bars(5), session=MagicMock())
    assert a.volatility.ewma_vol_pct is None and a.volatility.realized_vol_21d_pct is None
    assert a.daily_sigma_pct is None


def test_last_bar_date_lags_as_of_over_a_weekend():
    """The header shows the last bar used, not the requested date: on a
    weekend, or in a UTC container after the US close, ``as_of`` runs
    ahead of the data."""
    bars = _bars(300)
    last = bars.index[-1].date()
    with patch.object(engine_mod, "sector_relative_return", return_value=None):
        a = analyze_symbol("TEST", bars=bars, as_of=last + timedelta(days=2), session=MagicMock())
    assert a.as_of == last + timedelta(days=2)
    assert a.last_bar_date == last
