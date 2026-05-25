"""Unit tests for the v2 reversal indicators + the v2 composite.

Pure helpers are exercised directly (no DB). Sign convention throughout:
positive = bottom/bullish, negative = top/bearish.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from stockscan.technical.indicators.pivot_proximity import _pivot_values
from stockscan.technical.indicators.reversal_trigger import _reversal_values
from stockscan.technical.indicators.trend_location import _trend_values
from stockscan.technical.indicators.volume_confirm import _volume_values
from stockscan.technical.score import compute_technical_score


def _bars(closes, highs=None, lows=None, vols=None, sym="TEST"):
    n = len(closes)
    idx = pd.date_range("2023-01-02", periods=n, freq="B", tz="UTC")
    df = pd.DataFrame(
        {
            "open": closes,
            "high": highs or [c + 1 for c in closes],
            "low": lows or [c - 1 for c in closes],
            "close": closes,
            "adj_close": closes,
            "volume": vols or [1_000_000] * n,
            "symbol": [sym] * n,
        },
        index=idx,
    )
    df.attrs["symbol"] = sym
    return df


# ======================================================================
# reversal_trigger
# ======================================================================
class TestReversalTrigger:
    def _rv(self, closes):
        return _reversal_values(pd.Series(closes, dtype=float), rsi_period=2, os=10, ob=90)

    def test_bottom_turn_is_positive(self):
        closes = list(np.linspace(120, 90, 30)) + [93.0]  # deep down, then up bar
        v = self._rv(closes)
        assert v is not None and v["raw"] > 0.5

    def test_top_turn_is_negative(self):
        closes = list(np.linspace(80, 110, 30)) + [107.0]  # strong up, then down bar
        v = self._rv(closes)
        assert v is not None and v["raw"] < -0.5

    def test_oversold_but_still_falling_is_neutral(self):
        closes = list(np.linspace(120, 88, 32))  # strictly down through the last bar
        v = self._rv(closes)
        assert v is not None and v["raw"] == pytest.approx(0.0, abs=1e-9)

    def test_abstains_on_short_history(self):
        assert self._rv([100.0] * 10) is None


# ======================================================================
# pivot_proximity
# ======================================================================
class TestPivotProximity:
    def _pv(self, closes, highs=None, lows=None):
        c = pd.Series(closes, dtype=float)
        h = pd.Series(highs if highs is not None else [x + 0.5 for x in closes], dtype=float)
        lo = pd.Series(lows if lows is not None else [x - 0.5 for x in closes], dtype=float)
        return _pivot_values(h, lo, c, k=3, lookback=60, prox_atr=1.5, atr_period=14)

    def test_near_support_is_positive(self):
        # Flat ~100 with a clean trough (swing low) at index 30; price walks back
        # DOWN to that shelf over several bars and hooks up. The last few bars sit
        # at support (not at the resistance shelf above), so the 3-bar proximity
        # window reads "at support" — the dip-then-hook a V-bottom actually makes.
        closes = [100.0] * 70
        lows = [c - 0.3 for c in closes]
        lows[30] = 96.0  # confirmed swing low (k=3 bars of higher lows on each side)
        closes[-4:] = [98.0, 96.8, 96.2, 96.8]  # descend into support, then hook up
        lows[-4:] = [97.7, 96.5, 95.9, 96.4]
        v = self._pv(closes, lows=lows)
        assert v is not None and v["raw"] > 0

    def test_near_resistance_is_negative(self):
        closes = [100.0] * 70
        highs = [c + 0.3 for c in closes]
        highs[30] = 104.0  # confirmed swing high
        closes[-4:] = [102.0, 103.2, 103.8, 103.2]  # rally into resistance, then hook down
        highs[-4:] = [102.3, 103.5, 104.1, 103.5]
        v = self._pv(closes, highs=highs)
        assert v is not None and v["raw"] < 0

    def test_midair_is_neutral(self):
        closes = list(np.linspace(80, 140, 70))  # monotonic: no level near the last close
        v = self._pv(closes)
        assert v is not None and abs(v["raw"]) < 0.2

    def test_abstains_on_short_history(self):
        assert self._pv([100.0] * 40) is None

    def test_no_lookahead_recent_pivot_not_confirmed(self):
        # A swing low in the last (k) bars must NOT be used (right shoulder absent).
        closes = [100.0] * 70
        lows = [c - 0.3 for c in closes]
        lows[-2] = 95.0  # would be a support, but it's within k=3 of the end → unconfirmed
        closes[-1] = 95.5
        v = self._pv(closes, lows=lows)
        # No confirmed support near price → support not picked up → raw ~ 0 (not strongly +).
        assert v is not None and v.get("support") != 95.0


# ======================================================================
# trend_location
# ======================================================================
class TestTrendLocation:
    def _tv(self, closes):
        return _trend_values(
            pd.Series(closes, dtype=float),
            pos50_band=0.10,
            pos200_band=0.25,
            slope_band=0.05,
            slope_window=20,
        )

    def test_uptrend_positive(self):
        v = self._tv(list(np.linspace(80, 130, 120)))
        assert v is not None and v["raw"] > 0.5

    def test_downtrend_negative(self):
        v = self._tv(list(np.linspace(130, 80, 120)))
        assert v is not None and v["raw"] < -0.5

    def test_pos200_dropped_under_220_bars(self):
        v = self._tv(list(np.linspace(80, 130, 100)))  # < 220 bars
        assert v is not None and "pos200" not in v

    def test_pos200_present_with_full_history(self):
        v = self._tv(list(np.linspace(80, 130, 260)))
        assert v is not None and "pos200" in v

    def test_abstains_under_60_bars(self):
        assert self._tv([100.0] * 50) is None


# ======================================================================
# volume_confirm (symmetric)
# ======================================================================
class TestVolumeConfirm:
    def _vv(self, closes, highs, lows, vols):
        return _volume_values(
            pd.Series(highs, dtype=float),
            pd.Series(lows, dtype=float),
            pd.Series(closes, dtype=float),
            pd.Series(vols, dtype=float),
            rvol_window=50,
            spike_mult=1.5,
            vol_floor=0.5,
        )

    def test_bottom_climax_high_multiplier(self):
        n = 60
        closes = [100.0] * n
        highs = [c + 0.5 for c in closes]
        lows = [c - 0.5 for c in closes]
        vols = [1_000_000] * n
        # Last bar: DOWN close, huge volume, closes near its HIGH (rejected the low).
        closes[-1] = 99.0
        lows[-1] = 96.0
        highs[-1] = 99.2
        vols[-1] = 5_000_000
        v = self._vv(closes, highs, lows, vols)
        assert v is not None and v["multiplier"] > 0.8

    def test_top_climax_high_multiplier(self):
        n = 60
        closes = [100.0] * n
        highs = [c + 0.5 for c in closes]
        lows = [c - 0.5 for c in closes]
        vols = [1_000_000] * n
        # Last bar: UP close, huge volume, closes near its LOW (rejected the high).
        closes[-1] = 101.0
        highs[-1] = 104.0
        lows[-1] = 100.8
        vols[-1] = 5_000_000
        v = self._vv(closes, highs, lows, vols)
        assert v is not None and v["multiplier"] > 0.8

    def test_quiet_bar_floors_multiplier(self):
        n = 60
        closes = [100.0 + 0.01 * i for i in range(n)]
        highs = [c + 0.5 for c in closes]
        lows = [c - 0.5 for c in closes]
        vols = [1_000_000] * n  # no spike
        v = self._vv(closes, highs, lows, vols)
        assert v is not None and v["multiplier"] == pytest.approx(0.5, abs=1e-9)

    def test_abstains_on_short_history(self):
        n = 40
        closes = [100.0] * n
        v = self._vv(closes, [c + 1 for c in closes], [c - 1 for c in closes], [1e6] * n)
        assert v is None


# ======================================================================
# v2 composite — signed bottom/top + confirmation attenuation
# ======================================================================
class TestV2Composite:
    def _bottom_bars(self):
        base = list(np.linspace(120, 100, 200))
        dip = list(np.linspace(100, 92, 8))
        closes = base + dip
        closes[-1] = 95.0  # hook back up
        n = len(closes)
        highs = [c + 1 for c in closes]
        lows = [c - 1 for c in closes]
        vols = [1_000_000] * n
        lows[-1], highs[-1], vols[-1] = 92.0, 95.5, 5_000_000  # climax: spike, close off low
        return _bars(closes, highs, lows, vols)

    def test_bottom_scores_positive(self):
        r = compute_technical_score(None, self._bottom_bars(), pd.Timestamp("2024-01-01").date())
        assert r is not None and r.score > 0.1
        assert r.breakdown["_meta"]["methodology_version"] == 2

    def test_top_scores_negative(self):
        base = list(np.linspace(80, 100, 200))
        rip = list(np.linspace(100, 108, 8))
        closes = base + rip
        closes[-1] = 105.0
        n = len(closes)
        highs = [c + 1 for c in closes]
        lows = [c - 1 for c in closes]
        vols = [1_000_000] * n
        highs[-1], lows[-1], vols[-1] = 108.0, 104.5, 5_000_000
        r = compute_technical_score(None, _bars(closes, highs, lows, vols), pd.Timestamp("2024-01-01").date())
        assert r is not None and r.score < -0.1

    def test_confirmation_attenuates_without_volume_spike(self):
        # Same bottom shape but NO volume spike → C floors at 0.5 → |score| smaller.
        b_spike = self._bottom_bars()
        b_quiet = self._bottom_bars().copy()
        b_quiet.iloc[-1, b_quiet.columns.get_loc("volume")] = 1_000_000  # remove the spike
        b_quiet.attrs["symbol"] = "TEST"
        as_of = pd.Timestamp("2024-01-01").date()
        spike = compute_technical_score(None, b_spike, as_of)
        quiet = compute_technical_score(None, b_quiet, as_of)
        assert spike is not None and quiet is not None
        assert spike.score > quiet.score  # the climax spike raises conviction
