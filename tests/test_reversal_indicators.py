"""Unit tests for the reversal indicator primitives + the strategy's score.

Primitives are plain functions in ``stockscan.indicators``; the reversal scoring
math lives in ``ReversalSwing.reversal_score`` (the strategy owns its math).
Pure functions are exercised directly (no DB). Sign convention throughout:
positive = bottom/bullish, negative = top/bearish.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from stockscan.indicators import (
    pivot_proximity,
    reversal_trigger,
    trend_location,
    volume_confirm,
)
from stockscan.strategies.reversal_swing import ReversalSwing


def _score(bars, as_of):
    return ReversalSwing().reversal_score(bars, as_of)


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
        return reversal_trigger(pd.Series(closes, dtype=float), rsi_period=2, os=10, ob=90)

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
        return pivot_proximity(h, lo, c, k=3, lookback=60, prox_atr=1.5, atr_period=14)

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
        return trend_location(
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
        return volume_confirm(
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

    # ------------------------------------------------------------------
    # v2 multi-bar Wyckoff behaviour: the climax often prints 1-3 bars
    # BEFORE the hook bar gates entry, so the single-bar v1 version was
    # missing it and the multiplier floored on most real trades.
    # ------------------------------------------------------------------
    def test_climax_two_bars_before_hook_still_detected(self):
        """v2 fix: climax bar at offset -3 (last bar quiet). v1 missed this."""
        n = 60
        closes = [100.0] * n
        highs = [c + 0.5 for c in closes]
        lows = [c - 0.5 for c in closes]
        vols = [1_000_000] * n
        # Bar at offset -3 is the climax: down close, huge vol, closes upper half
        # of a wide range. prev close = 100, this close = 99, low = 95, high = 99.5.
        # clv ≈ 0.78, spread_ratio = 4.5 (wide), spike_s saturates → score ≈ 0.78.
        closes[-3] = 99.0
        highs[-3] = 99.5
        lows[-3] = 95.0
        vols[-3] = 5_000_000
        # Last two bars: quiet normal bars — they MUST be skipped (rvol = 1.0 < spike_mult).
        v = self._vv(closes, highs, lows, vols)
        assert v is not None
        assert v["multiplier"] > 0.8
        assert v["climax_offset"] == -3
        assert v["climax_kind"] == "climax"
        assert v["climax_direction"] == "bullish"

    def test_wide_spread_scores_higher_than_normal_spread(self):
        """Same volume + same close-location, wider spread = stronger climax.
        spread_factor: wide=1.0, mixed=0.5 — the wide-spread climax dominates."""
        n = 60
        # Wide-spread setup: rng = 4 vs baseline median 1.0 → "climax" kind.
        closes_w = [100.0] * n
        highs_w = [c + 0.5 for c in closes_w]
        lows_w = [c - 0.5 for c in closes_w]
        vols_w = [1_000_000] * n
        closes_w[-1] = 99.0
        highs_w[-1] = 99.5
        lows_w[-1] = 95.5  # rng = 4, clv ≈ 0.75
        vols_w[-1] = 5_000_000

        # Normal-spread setup: rng = 1 (= baseline median) → "mixed" kind.
        closes_n = [100.0] * n
        highs_n = [c + 0.5 for c in closes_n]
        lows_n = [c - 0.5 for c in closes_n]
        vols_n = [1_000_000] * n
        closes_n[-1] = 99.8
        highs_n[-1] = 100.0
        lows_n[-1] = 99.0  # rng = 1, clv = ((99.8-99)-(100-99.8))/1 = 0.6
        vols_n[-1] = 5_000_000

        v_wide = self._vv(closes_w, highs_w, lows_w, vols_w)
        v_normal = self._vv(closes_n, highs_n, lows_n, vols_n)
        assert v_wide is not None and v_normal is not None
        assert v_wide["climax_kind"] == "climax"
        assert v_normal["climax_kind"] == "mixed"
        assert v_wide["multiplier"] > v_normal["multiplier"]

    def test_absorption_pattern_detected(self):
        """Narrow-spread + high vol + close-rejection = Wyckoff absorption.
        Quieter than a climax (spread_factor 0.85) but still reversal-confirming."""
        n = 60
        closes = [100.0] * n
        highs = [c + 0.5 for c in closes]  # baseline median spread = 1.0
        lows = [c - 0.5 for c in closes]
        vols = [1_000_000] * n
        # Last bar: down close, huge vol, NARROW range (0.5 < 0.7 × 1.0).
        # high=100, low=99.5, close=99.95 → spread_ratio 0.5, clv = 0.8 (near high).
        closes[-1] = 99.95
        highs[-1] = 100.0
        lows[-1] = 99.5
        vols[-1] = 5_000_000
        v = self._vv(closes, highs, lows, vols)
        assert v is not None
        assert v["climax_kind"] == "absorption"
        assert v["climax_direction"] == "bullish"
        # spread_factor=0.85 vs wide=1.0 → multiplier slightly below full climax
        # but still well above floor (0.5).
        assert v["multiplier"] > 0.7

    def test_continuation_bar_not_treated_as_climax(self):
        """Down day closing near its LOW (no rejection) is a continuation, not
        a climax — even with huge volume. Multiplier must floor."""
        n = 60
        closes = [100.0] * n
        highs = [c + 0.5 for c in closes]
        lows = [c - 0.5 for c in closes]
        vols = [1_000_000] * n
        # Down day, wide range, closes at the LOW (clv ≈ -1) → continuation.
        closes[-1] = 96.0
        highs[-1] = 100.0
        lows[-1] = 95.8  # close near low
        vols[-1] = 5_000_000
        v = self._vv(closes, highs, lows, vols)
        assert v is not None
        # No qualifying candidate found → score 0 → multiplier at vol_floor.
        assert v["multiplier"] == pytest.approx(0.5, abs=1e-9)
        assert v["climax_kind"] == "none"


# ======================================================================
# v2 composite — signed bottom/top + confirmation attenuation
# ======================================================================
class TestV2Composite:
    def _bottom_bars(self):
        # v1.4.0 added a pivot_proximity floor gate, so the fixture needs a
        # confirmed swing low BELOW the eventual hook close in the trailing
        # 60-bar window. Insert a V-shape inside the descending base.
        base = list(np.linspace(120, 100, 200))
        # 7-bar V at indices 175-181: confirmed swing low at idx 178
        # (close 94, low 93) with k=3 higher lows on each side.
        base[175:182] = [98.0, 96.0, 95.0, 94.0, 95.0, 96.0, 98.0]
        dip = list(np.linspace(100, 92, 8))
        closes = base + dip
        closes[-1] = 95.0  # hook back up
        n = len(closes)
        highs = [c + 1 for c in closes]
        lows = [c - 1 for c in closes]
        vols = [1_000_000] * n
        # Wyckoff selling climax on bar -2 (the panic-low bar, close 92):
        # wide spread + huge volume + close in the upper half of its range
        # (rejection of the low). The LAST bar (the hook, close 95) is normal
        # vol — it's the confirmation, not the climax. This shape is what
        # v2 volume_confirm is designed to detect: climax prints 1-2 bars
        # BEFORE the hook gates entry.
        lows[-2], highs[-2], vols[-2] = 88.0, 93.0, 5_000_000
        return _bars(closes, highs, lows, vols)

    def test_bottom_scores_positive(self):
        r = _score(self._bottom_bars(), pd.Timestamp("2024-01-01").date())
        assert r is not None and r.score > 0.1
        assert r.breakdown["_meta"]["methodology_version"] == 2

    def test_top_setup_returns_none_after_v1_2_0_gate(self):
        """v1.2.0 added a hard gate inside reversal_score() that rejects any
        setup where reversal_trigger.raw is None or ≤ 0. Top setups (where the
        primitive's raw is negative) therefore return None — the strategy no
        longer scores top-side bottoms via this method. Exits are carried by
        the ATR hard stop and the time stop exclusively; the reversal_top exit
        branch is unreachable. See ReversalSwing.manual for the rationale."""
        base = list(np.linspace(80, 100, 200))
        rip = list(np.linspace(100, 108, 8))
        closes = base + rip
        closes[-1] = 105.0
        n = len(closes)
        highs = [c + 1 for c in closes]
        lows = [c - 1 for c in closes]
        vols = [1_000_000] * n
        highs[-1], lows[-1], vols[-1] = 108.0, 104.5, 5_000_000
        r = _score(_bars(closes, highs, lows, vols), pd.Timestamp("2024-01-01").date())
        assert r is None

    def test_confirmation_attenuates_without_volume_spike(self):
        # Same bottom shape but NO volume spike on the climax bar → multiplier
        # floors → |score| smaller. v2: the climax bar is at offset -2 (the
        # panic-low bar before the hook), so that's where the spike sits.
        b_spike = self._bottom_bars()
        b_quiet = self._bottom_bars().copy()
        b_quiet.iloc[-2, b_quiet.columns.get_loc("volume")] = 1_000_000  # remove climax-bar spike
        b_quiet.attrs["symbol"] = "TEST"
        as_of = pd.Timestamp("2024-01-01").date()
        spike = _score(b_spike, as_of)
        quiet = _score(b_quiet, as_of)
        assert spike is not None and quiet is not None
        assert spike.score > quiet.score  # the climax spike raises conviction
