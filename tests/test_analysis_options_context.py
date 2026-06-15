"""Tests for the multi-tenor strike ladder + strike-confluence callout.

Covers:
  * ``compute_trend`` populates the EMA dict (periods from _EMA_PERIODS).
  * ``compute_options_context`` emits one StrikeSet per configured tenor,
    with the right days-to-expiry, target delta, expiry date, and OTM
    direction (put below spot, call above).
  * ``_strike_confluences`` flags an EMA / S/R level within 0.5×ATR and
    ignores ones outside the band (and no-ops when ATR is unknown).
  * The confluence shows up end-to-end on the produced OptionStrike and in
    the observations.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from stockscan.analysis.options_context import (
    _CONFLUENCE_ATR_MULT,
    _STRIKE_TENORS,
    _strike_confluences,
    compute_options_context,
)
from stockscan.analysis.state import Level, TrendState, VolatilityState
from stockscan.analysis.trend import _EMA_PERIODS, compute_trend


def _bars(n: int = 300, base: float = 100.0, seed: int = 1) -> pd.DataFrame:
    idx = pd.date_range("2023-01-02", periods=n, freq="B")
    rng = np.random.default_rng(seed)
    closes = base + np.cumsum(rng.normal(0, 0.1, n))
    return pd.DataFrame(
        {
            "open": closes,
            "high": closes + np.abs(rng.normal(0, 0.2, n)),
            "low": closes - np.abs(rng.normal(0, 0.2, n)),
            "close": closes,
            "volume": rng.integers(1_000_000, 5_000_000, n),
        },
        index=idx,
    )


def _vol_state(atr_14: float | None = 3.0, ewma_vol_pct: float | None = 22.0) -> VolatilityState:
    return VolatilityState(
        available=True, realized_vol_21d_pct=28.5, realized_vol_63d_pct=26.0,
        atr_14=atr_14, atr_pct_of_price=2.0, bb_width_pct=8.0, hv_percentile=55.0,
        expected_7d=None, expected_30d=None, bucket="normal", label="Normal",
        explanation="", ewma_vol_pct=ewma_vol_pct,
    )


# ---------------------------------------------------------------------------
# EMA on TrendState
# ---------------------------------------------------------------------------
def test_compute_trend_populates_emas():
    t = compute_trend(_bars())
    assert t.available
    assert set(t.emas) == set(_EMA_PERIODS)
    for period in _EMA_PERIODS:
        assert t.emas[period] is not None and t.emas[period] > 0


# ---------------------------------------------------------------------------
# Multi-tenor strike ladder
# ---------------------------------------------------------------------------
def test_strike_sets_match_configured_tenors():
    ctx = compute_options_context(
        symbol="AAPL", as_of=date(2026, 6, 13), last_close=150.0,
        levels=[], trend=TrendState.unavailable(), volatility=_vol_state(),
        session=None,
    )
    assert len(ctx.strike_sets) == len(_STRIKE_TENORS)
    for ss, (days, delta) in zip(ctx.strike_sets, _STRIKE_TENORS, strict=True):
        assert ss.days_to_expiry == days
        assert ss.target_delta == delta
        assert ss.expiry_date == date(2026, 6, 13) + pd.Timedelta(days=days).to_pytimedelta()
        # Put below spot, call above; deltas carry the right sign.
        assert ss.put.strike < 150.0 < ss.call.strike
        assert ss.call.delta > 0 and ss.put.delta < 0
        assert abs(ss.call.delta) == round(delta, 4)


def test_expiries_land_on_the_two_fridays_from_a_saturday():
    # Sat 2026-06-13: 6 days → Fri 06-19, 13 days → Fri 06-26.
    ctx = compute_options_context(
        symbol="X", as_of=date(2026, 6, 13), last_close=100.0,
        levels=[], trend=TrendState.unavailable(), volatility=_vol_state(),
        session=None,
    )
    by_days = {ss.days_to_expiry: ss for ss in ctx.strike_sets}
    assert by_days[6].expiry_date == date(2026, 6, 19)
    assert by_days[6].expiry_date.weekday() == 4  # Friday
    assert by_days[13].expiry_date == date(2026, 6, 26)
    assert by_days[13].expiry_date.weekday() == 4


def test_no_strike_sets_without_vol():
    ctx = compute_options_context(
        symbol="X", as_of=date(2026, 6, 13), last_close=100.0,
        levels=[], trend=TrendState.unavailable(),
        volatility=VolatilityState.unavailable(), session=None,
    )
    assert ctx.strike_sets == []


# ---------------------------------------------------------------------------
# Confluence helper
# ---------------------------------------------------------------------------
def _trend_with_ema(period: int, value: float) -> TrendState:
    return TrendState(
        available=True, bucket="neutral", label="", explanation="",
        return_5d=None, return_21d=None, return_63d=None, ma_alignment="mixed",
        sma_20=None, sma_50=None, sma_200=None, adx_14=None,
        pct_above_sma20=None, pct_above_sma50=None, pct_above_sma200=None,
        emas={period: value},
    )


def _level(price: float, kind: str = "resistance") -> Level:
    return Level(price=price, kind=kind, strength=0.8, touches=3,
                 last_touch_days_ago=5, distance_pct=1.0,
                 origin="pivot_high" if kind == "resistance" else "pivot_low")


def test_confluence_flags_nearby_ema():
    # band = 0.5 * ATR(2.0) = 1.0; EMA 0.5 away → hit.
    out = _strike_confluences(
        strike=100.0, levels=[], trend=_trend_with_ema(50, 100.5), atr14=2.0
    )
    assert any("50 EMA" in c for c in out)


def test_confluence_ignores_far_ema():
    # EMA 5 away, band = 1.0 → no hit.
    out = _strike_confluences(
        strike=100.0, levels=[], trend=_trend_with_ema(50, 105.0), atr14=2.0
    )
    assert out == ()


def test_confluence_flags_nearby_level():
    out = _strike_confluences(
        strike=100.0, levels=[_level(100.8), _level(120.0)],
        trend=TrendState.unavailable(), atr14=2.0,
    )
    assert len(out) == 1
    assert "resistance $100.80" in out[0]


def test_confluence_noop_without_atr():
    assert _strike_confluences(
        strike=100.0, levels=[_level(100.0)],
        trend=_trend_with_ema(50, 100.0), atr14=None,
    ) == ()


def test_confluence_band_uses_configured_mult():
    # Sanity: the helper's band is exactly _CONFLUENCE_ATR_MULT * ATR.
    atr = 4.0
    band = _CONFLUENCE_ATR_MULT * atr
    just_in = _strike_confluences(
        strike=100.0, levels=[], trend=_trend_with_ema(9, 100.0 + band - 0.01), atr14=atr
    )
    just_out = _strike_confluences(
        strike=100.0, levels=[], trend=_trend_with_ema(9, 100.0 + band + 0.01), atr14=atr
    )
    assert just_in and not just_out


def test_confluence_surfaces_end_to_end():
    # Put an S/R level right on top of the 30-day call strike and confirm it
    # reaches both the OptionStrike.confluences and the observations.
    ctx = compute_options_context(
        symbol="AAPL", as_of=date(2026, 6, 13), last_close=150.0,
        levels=[], trend=TrendState.unavailable(), volatility=_vol_state(atr_14=3.0),
        session=None,
    )
    call_30 = {ss.days_to_expiry: ss for ss in ctx.strike_sets}[30].call
    # Now rebuild with a level sitting on that strike.
    ctx2 = compute_options_context(
        symbol="AAPL", as_of=date(2026, 6, 13), last_close=150.0,
        levels=[_level(call_30.strike)], trend=TrendState.unavailable(),
        volatility=_vol_state(atr_14=3.0), session=None,
    )
    call_30b = {ss.days_to_expiry: ss for ss in ctx2.strike_sets}[30].call
    assert call_30b.confluences
    assert any("confluence" in o.lower() for o in ctx2.observations)


# ---------------------------------------------------------------------------
# Vol source + FRED risk-free rate
# ---------------------------------------------------------------------------
def test_strikes_use_ewma_forward_vol():
    ctx = compute_options_context(
        symbol="X", as_of=date(2026, 6, 13), last_close=150.0, levels=[],
        trend=TrendState.unavailable(),
        volatility=_vol_state(ewma_vol_pct=20.0), session=None,
    )
    # Every leg should be priced off the EWMA forward vol (20.0), not the
    # trailing 21-day HV (28.5).
    for ss in ctx.strike_sets:
        assert ss.call.vol_pct == 20.0
        assert ss.put.vol_pct == 20.0


def test_strikes_fall_back_to_trailing_hv_without_ewma():
    ctx = compute_options_context(
        symbol="X", as_of=date(2026, 6, 13), last_close=150.0, levels=[],
        trend=TrendState.unavailable(),
        volatility=_vol_state(ewma_vol_pct=None), session=None,
    )
    for ss in ctx.strike_sets:
        assert ss.call.vol_pct == 28.5  # realized_vol_21d_pct


def test_risk_free_rate_fallback_without_session():
    from stockscan.analysis.options_context import _risk_free_rate
    from stockscan.config import settings
    assert _risk_free_rate(date(2026, 6, 13), None) == settings.risk_free_rate


def test_risk_free_rate_from_macro_series(monkeypatch):
    from decimal import Decimal

    import stockscan.analysis.options_context as oc

    # FRED stores DGS1MO in percent → 5.20 means 5.20% → 0.052.
    monkeypatch.setattr(oc, "latest_macro_value", lambda *a, **k: Decimal("5.20"))
    assert oc._risk_free_rate(date(2026, 6, 13), object()) == pytest.approx(0.052)

    # Implausible value (data error) → fall back to config.
    monkeypatch.setattr(oc, "latest_macro_value", lambda *a, **k: Decimal("999"))
    assert oc._risk_free_rate(date(2026, 6, 13), object()) == oc.settings.risk_free_rate

    # Missing print → fall back.
    monkeypatch.setattr(oc, "latest_macro_value", lambda *a, **k: None)
    assert oc._risk_free_rate(date(2026, 6, 13), object()) == oc.settings.risk_free_rate
