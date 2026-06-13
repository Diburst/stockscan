"""Contract test for the interactive-chart payload builder.

Locks the shape the front end (``analysis/detail.html``) depends on:
required top-level keys, required study keys, default-on set, and
the per-study sub-shape. If a future indicator-surface change breaks
any of these expectations, this test fails loudly.

No DB needed — builds bars + a manually-constructed ``SymbolAnalysis``
and passes both into ``build_chart_payload`` directly.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from stockscan.analysis.chart_data import DEFAULT_STUDIES, build_chart_payload
from stockscan.analysis.state import (
    ExpectedRange,
    Level,
    MomentumState,
    OptionsContext,
    SymbolAnalysis,
    TrendState,
    VolatilityState,
)


@pytest.fixture
def synthetic_bars() -> pd.DataFrame:
    """400 business days of synthetic OHLCV bars — enough for SMA(200) warmup."""
    idx = pd.date_range("2024-01-01", periods=400, freq="B")
    rng = np.random.default_rng(42)
    closes = 100 + np.cumsum(rng.normal(0, 1, 400))
    return pd.DataFrame(
        {
            "open": closes + rng.normal(0, 0.5, 400),
            "high": closes + 1.5,
            "low": closes - 1.5,
            "close": closes,
            "adj_close": closes,
            "volume": rng.integers(100_000, 1_000_000, 400),
        },
        index=idx,
    )


@pytest.fixture
def analysis(synthetic_bars: pd.DataFrame) -> SymbolAnalysis:
    """A minimal SymbolAnalysis with levels + expected-move bands populated."""
    last = synthetic_bars.index[-1].date()
    last_close = float(synthetic_bars["close"].iloc[-1])
    return SymbolAnalysis(
        symbol="TEST",
        as_of=last,
        available=True,
        last_close=last_close,
        last_volume=100_000.0,
        bars_count=len(synthetic_bars),
        levels=[
            Level(price=last_close - 5, kind="support", strength=0.7,
                  touches=3, last_touch_days_ago=10, distance_pct=-5.0,
                  origin="pivot_low"),
            Level(price=last_close + 8, kind="resistance", strength=0.5,
                  touches=2, last_touch_days_ago=20, distance_pct=8.0,
                  origin="pivot_high"),
        ],
        trend=TrendState.unavailable(),
        volatility=VolatilityState(
            available=True, realized_vol_21d_pct=20.0, realized_vol_63d_pct=22.0,
            atr_14=1.5, atr_pct_of_price=1.5, bb_width_pct=4.0, hv_percentile=50.0,
            expected_7d=ExpectedRange(horizon_days=7, sigma_pct=2.0,
                                       low=last_close * 0.98,
                                       high=last_close * 1.02,
                                       sigma_dollars=last_close * 0.02),
            expected_30d=ExpectedRange(horizon_days=30, sigma_pct=5.0,
                                        low=last_close * 0.95,
                                        high=last_close * 1.05,
                                        sigma_dollars=last_close * 0.05),
            bucket="normal", label="normal", explanation="",
        ),
        momentum=MomentumState.unavailable(),
        options_context=OptionsContext.unavailable(),
    )


def test_top_level_shape(synthetic_bars, analysis):
    """The keys analysis/detail.html unpacks must all be present."""
    payload = build_chart_payload("TEST", analysis, bars=synthetic_bars)
    for key in ("symbol", "as_of", "last_close", "bars", "studies",
                "levels", "expected_move", "fib_retracement", "default_on"):
        assert key in payload, f"missing top-level key: {key}"
    assert payload["symbol"] == "TEST"
    assert payload["bars"], "bars should not be empty for valid input"


def test_level_carries_confirmed_by_weekly(synthetic_bars, analysis):
    """Every level in the payload exposes confirmed_by_weekly to the front end."""
    payload = build_chart_payload("TEST", analysis, bars=synthetic_bars)
    for lv in payload["levels"]:
        assert "confirmed_by_weekly" in lv
        assert isinstance(lv["confirmed_by_weekly"], bool)


def test_fib_retracement_payload(synthetic_bars, analysis):
    """Fibonacci retracements ride along when the bars cover the lookback."""
    payload = build_chart_payload("TEST", analysis, bars=synthetic_bars)
    fib = payload["fib_retracement"]
    # 400 bars > default lookback of 120 → fib should be populated.
    assert fib is not None
    assert fib["high"] > fib["low"]
    assert fib["direction"] in ("down_from_high", "up_from_low")
    assert len(fib["levels"]) == 5
    # ISO date strings — front-end never parses dates from this payload
    # but we promise YYYY-MM-DD to keep templates simple.
    assert len(fib["high_date"]) == 10 and fib["high_date"][4] == "-"
    assert len(fib["low_date"]) == 10 and fib["low_date"][4] == "-"


def test_bar_record_shape(synthetic_bars, analysis):
    """Each bar must carry the Lightweight-Charts OHLCV fields."""
    payload = build_chart_payload("TEST", analysis, bars=synthetic_bars)
    bar = payload["bars"][0]
    for k in ("time", "open", "high", "low", "close", "volume"):
        assert k in bar, f"bar record missing {k}"
    # `time` is the YYYY-MM-DD string Lightweight Charts expects.
    assert len(bar["time"]) == 10 and bar["time"][4] == "-"


def test_chart_history_cap(analysis):
    """Bars are capped at ~756 trading days (~3y) so the range buttons can
    offer a 3y window without sending unbounded history."""
    from stockscan.analysis.chart_data import _CHART_HISTORY_DAYS

    # Build a series LONGER than the cap so capping is actually exercised.
    n = _CHART_HISTORY_DAYS + 200
    idx = pd.date_range("2018-01-01", periods=n, freq="B")
    rng = np.random.default_rng(7)
    closes = 100 + np.cumsum(rng.normal(0, 1, n))
    long_bars = pd.DataFrame(
        {
            "open": closes + rng.normal(0, 0.5, n),
            "high": closes + 1.5,
            "low": closes - 1.5,
            "close": closes,
            "adj_close": closes,
            "volume": rng.integers(100_000, 1_000_000, n),
        },
        index=idx,
    )
    # Use an as_of past the end of the synthetic frame so no rows are trimmed.
    capped_analysis = SymbolAnalysis(
        symbol="TEST",
        as_of=long_bars.index[-1].date(),
        available=True,
        last_close=float(long_bars["close"].iloc[-1]),
        last_volume=100_000.0,
        bars_count=n,
        levels=[],
        trend=analysis.trend,
        volatility=analysis.volatility,
        momentum=analysis.momentum,
        options_context=analysis.options_context,
    )
    payload = build_chart_payload("TEST", capped_analysis, bars=long_bars)
    assert len(payload["bars"]) == _CHART_HISTORY_DAYS


def test_all_documented_studies_present(synthetic_bars, analysis):
    """Every key the front-end's STUDY_DEFS table references must exist."""
    payload = build_chart_payload("TEST", analysis, bars=synthetic_bars)
    expected_studies = {
        "sma_20", "sma_50", "sma_200",
        "ema_20", "ema_50", "ema_200",
        "bb", "donchian", "atr_bands",
        "volume", "rsi_14", "macd",
    }
    assert set(payload["studies"].keys()) == expected_studies


def test_default_on_set(synthetic_bars, analysis):
    """First-open defaults per Thomas's spec: SMA50, SMA200, vol, S/R, move."""
    payload = build_chart_payload("TEST", analysis, bars=synthetic_bars)
    assert set(payload["default_on"]) == set(DEFAULT_STUDIES)
    assert "sma_50" in payload["default_on"]
    assert "sma_200" in payload["default_on"]
    assert "volume" in payload["default_on"]
    assert "levels" in payload["default_on"]
    assert "expected_move" in payload["default_on"]


def test_line_study_shape(synthetic_bars, analysis):
    """Line studies (SMAs / EMAs / RSI) carry `data` as [{time, value}, ...]."""
    payload = build_chart_payload("TEST", analysis, bars=synthetic_bars)
    for key in ("sma_50", "sma_200", "ema_20", "rsi_14"):
        st = payload["studies"][key]
        assert st["kind"] in ("line", "subpanel_line")
        assert "color" in st
        assert st["data"], f"{key} should have data with 400 bars of input"
        # Each point: {time, value}
        sample = st["data"][-1]
        assert set(sample.keys()) == {"time", "value"}


def test_band_study_shape(synthetic_bars, analysis):
    """Bollinger / Donchian / ATR bands carry upper+middle+lower lists."""
    payload = build_chart_payload("TEST", analysis, bars=synthetic_bars)
    for key in ("bb", "donchian", "atr_bands"):
        st = payload["studies"][key]
        assert st["kind"] == "band"
        for part in ("upper", "middle", "lower"):
            assert st[part], f"{key}.{part} should have data"
            # Bollinger upper > middle > lower at most points (sanity).
        # Sample the last bar — upper should be > lower for valid stddev.
        if key == "bb":
            last_upper = st["upper"][-1]["value"]
            last_lower = st["lower"][-1]["value"]
            assert last_upper > last_lower


def test_macd_subpanel_shape(synthetic_bars, analysis):
    """MACD payload has line + signal + histogram series."""
    payload = build_chart_payload("TEST", analysis, bars=synthetic_bars)
    macd = payload["studies"]["macd"]
    assert macd["kind"] == "subpanel_macd"
    for part in ("line", "signal", "histogram"):
        assert macd[part], f"macd.{part} should have data"


def test_rsi_ref_lines(synthetic_bars, analysis):
    """RSI ships with 70 / 30 overbought / oversold reference lines."""
    payload = build_chart_payload("TEST", analysis, bars=synthetic_bars)
    rsi = payload["studies"]["rsi_14"]
    prices = {rl["price"] for rl in rsi["ref_lines"]}
    assert prices == {70.0, 30.0}


def test_volume_color_by_direction(synthetic_bars, analysis):
    """Volume bars are colored green on up-days, red on down-days."""
    payload = build_chart_payload("TEST", analysis, bars=synthetic_bars)
    vol = payload["studies"]["volume"]
    assert vol["kind"] == "subpanel_volume"
    colors = {rec["color"] for rec in vol["data"]}
    # Both colors should appear over 252 random bars (with overwhelming probability).
    assert any("5,150,105" in c for c in colors)  # green
    assert any("220,38,38" in c for c in colors)  # red


def test_levels_pass_through(synthetic_bars, analysis):
    """S/R levels from the SymbolAnalysis flow into the payload."""
    payload = build_chart_payload("TEST", analysis, bars=synthetic_bars)
    assert len(payload["levels"]) == 2
    kinds = {lv["kind"] for lv in payload["levels"]}
    assert kinds == {"support", "resistance"}


def test_expected_move_pass_through(synthetic_bars, analysis):
    """7d/30d expected-move bands flow into the payload."""
    payload = build_chart_payload("TEST", analysis, bars=synthetic_bars)
    em = payload["expected_move"]
    assert "7d" in em and "30d" in em
    assert em["7d"]["high"] > em["7d"]["low"]
    assert em["30d"]["high"] > em["30d"]["low"]


def test_empty_bars_returns_empty_payload(analysis):
    """No bars → empty dict, so the template can render the SVG fallback."""
    empty = pd.DataFrame(
        {c: [] for c in ("open", "high", "low", "close", "adj_close", "volume")}
    )
    empty.index = pd.DatetimeIndex([])
    payload = build_chart_payload("TEST", analysis, bars=empty)
    assert payload == {}


def test_nan_warmup_is_skipped(synthetic_bars, analysis):
    """The first SMA(200) values are NaN; they must NOT appear as nulls in JSON."""
    payload = build_chart_payload("TEST", analysis, bars=synthetic_bars)
    # SMA(200) needs 200 bars of warmup; after the 252-cap, ~52 bars are valid.
    sma_200 = payload["studies"]["sma_200"]["data"]
    # No record may carry a non-finite value.
    for rec in sma_200:
        assert rec["value"] == rec["value"]  # not NaN
