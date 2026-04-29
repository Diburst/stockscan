"""Largecap Rebound (Thomas Z).

Reference implementation.

  Setup:    Close < SMA(200) AND symbol is in the top quintile of
            S&P 500 by market cap AND ADX(14) ≥ adx_min (default 20)
            so we only trade when a real directional move is forming.
  Entry:    RSI(14) ≥ threshold AND rising.
            MACD histogram > 0 AND rising.
  Exit (whichever first):
            MACD histogram ≤ 0           → momentum has rolled over;
                                            exit at next open
            Close ≤ entry − 2.5×ATR      → hard stop at next open

  No profit target, no time stop — winners ride until MACD turns over.
  The hard stop bounds single-trade downside at ~2.5R; without it, a
  slow MACD crossover on a steadily declining stock would compound losses.

  ADX filter rationale: counter-trend strategies whip in choppy markets
  because RSI/MACD generate frequent false signals when there's no real
  directional move. ADX(14) measures trend STRENGTH (not direction) — when
  it's below 20, the market is range-bound and these signals are usually
  noise. Skipping entries with low ADX cuts whipsaw losses substantially
  at the cost of being slightly late to genuine reversals.

The class subclasses Strategy, which auto-registers via __init_subclass__.
The contract tests in tests/test_strategy_contract.py run against this
strategy automatically.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import ClassVar

import pandas as pd
from pydantic import Field

from stockscan.fundamentals import market_cap_percentile
from stockscan.indicators import (
    adx as compute_adx,
)
from stockscan.indicators import (
    atr,
    sma,
)
from stockscan.indicators import (
    macd as compute_macd,
)
from stockscan.indicators import (
    rsi as compute_rsi,
)
from stockscan.strategies import (
    ExitDecision,
    PositionSnapshot,
    RawSignal,
    Strategy,
    StrategyParams,
)


class LargeCapReboundParams(StrategyParams):
    """Tunable parameters for Largecap Rebound."""

    market_cap_pct_floor: int = Field(
        80, ge=0, le=101, description="Filter out symbols below market cap threshold"
    )
    trend_sma_period: int = Field(200, ge=20, le=400, description="Long-term trend filter")
    rsi_period: int = Field(14, ge=1, le=20, description="RSI lookback window")
    rsi_threshold: float = Field(
        35.0, ge=30.0, le=80.0, description="Enter when RSI ≥ this AND rising"
    )
    macd_fast_ma: int = Field(12, ge=1, le=26, description="Fast moving average period")
    macd_slow_ma: int = Field(26, ge=12, le=50, description="Slow moving average period")
    # ge=1 — span=0 would crash pandas EMA. Practical range is ≥3.
    macd_signal: int = Field(9, ge=1, le=26, description="Signal period")

    # ADX trend-strength filter. Skip entries when the market isn't actually
    # moving (chop-resistance). Threshold below ~18 = range-bound; above ~25
    # = clear trend. 20 is a reasonable middle ground.
    adx_period: int = Field(14, ge=5, le=50, description="ADX lookback")
    adx_min: float = Field(
        20.0,
        ge=0.0,
        le=50.0,
        description="Skip entries when ADX is below this (chop filter)",
    )

    atr_period: int = Field(14, ge=5, le=50)
    atr_stop_mult: float = Field(2.5, ge=1.0, le=5.0, description="Hard-stop ATR multiple")


class LargeCapRebound(Strategy):
    name = "largecap_rebound"
    version = "1.0.0"
    display_name = "Largecap Rebound"
    description = (
        "A long-term recovery strategy that buys stocks on weakness "
        "by looking at positive technical momentum."
    )
    tags = ("mean_reversion", "long_only", "swing")
    params_model = LargeCapReboundParams
    default_risk_pct = 0.01
    # Largecap rebound buys quality names on weakness; needs a friendly tape
    # to actually recover. trending_up = best, trending_down = skip (catching
    # falling knives in real bears is a known disaster mode for this style).
    # choppy gets a small weight — rsi2_meanrev covers the chop case better,
    # so keep largecap_rebound mostly out of the way there.
    regime_affinity: ClassVar[dict[str, float]] = {
        "trending_up": 1.0,
        "trending_down": 0.0,
        "choppy": 0.4,
        "transitioning": 0.7,
    }

    manual = """\
## What this strategy is trying to do
WIN
"""

    # ------------------------------------------------------------------
    def required_history(self) -> int:
        # Need enough for the longest indicator chain: SMA(200) + MACD slow EMA
        # + signal smoothing, plus ADX warmup (Wilder smoothing requires roughly
        # 2× period for stable values), plus a buffer.
        return (
            self.params.trend_sma_period
            + self.params.macd_slow_ma
            + self.params.macd_signal
            + 2 * self.params.adx_period
            + 20
        )

    # ------------------------------------------------------------------
    def signals(self, bars: pd.DataFrame, as_of: date) -> list[RawSignal]:
        view = self._slice(bars, as_of)
        if len(view) < self.required_history():
            return []

        symbol = self._symbol(view)
        close = view["close"]
        high = view["high"]
        low = view["low"]

        # === SETUP FILTERS ===

        # 1. Market-cap filter. If we have no fundamentals row for this symbol,
        #    abstain (rather than incorrectly assuming pass/fail).
        if not self._is_large_cap(symbol, as_of):
            return []

        # 2. Long-term downtrend filter: close MUST BE BELOW SMA(200).
        sma_trend = sma(close, self.params.trend_sma_period).iloc[-1]
        last_close = float(close.iloc[-1])
        if pd.isna(sma_trend) or last_close >= float(sma_trend):
            return []

        # 3. ADX trend-strength filter — skip chop. ADX below threshold means
        #    the market isn't actually moving directionally; RSI/MACD signals
        #    here are usually noise that produces whipsaw losses.
        adx_v = compute_adx(high, low, close, self.params.adx_period).iloc[-1]
        if pd.isna(adx_v) or float(adx_v) < self.params.adx_min:
            return []

        # === ENTRY TRIGGERS ===

        # 4. RSI above threshold AND rising
        rsi_series = compute_rsi(close, self.params.rsi_period)
        rsi_today = float(rsi_series.iloc[-1])
        rsi_prev = float(rsi_series.iloc[-2])
        if pd.isna(rsi_today) or pd.isna(rsi_prev):
            return []
        if rsi_today < self.params.rsi_threshold:
            return []
        if rsi_today <= rsi_prev:
            return []  # not rising

        # 5. MACD histogram positive AND rising
        macd_df = compute_macd(
            close,
            self.params.macd_fast_ma,
            self.params.macd_slow_ma,
            self.params.macd_signal,
        )
        hist_today = float(macd_df["histogram"].iloc[-1])
        hist_prev = float(macd_df["histogram"].iloc[-2])
        if pd.isna(hist_today) or pd.isna(hist_prev):
            return []
        if hist_today <= 0:
            return []  # not bullish yet
        if hist_today > 1:
            return []  # rebound is underway, skip it
        if hist_today <= hist_prev:
            return []  # not accelerating

        # === SIZING / STOP ===
        atr_v = atr(high, low, close, self.params.atr_period).iloc[-1]
        if pd.isna(atr_v) or atr_v <= 0:
            return []

        entry = Decimal(str(round(last_close, 4)))
        stop = Decimal(str(round(last_close - self.params.atr_stop_mult * float(atr_v), 4)))

        # Score: weighted combo of how far below 200-SMA + RSI strength.
        # Further below SMA(200) and stronger momentum = higher score.
        dist_below_sma = (float(sma_trend) - last_close) / float(sma_trend)
        rsi_denom = max(1.0, 100 - self.params.rsi_threshold)
        rsi_strength = (rsi_today - self.params.rsi_threshold) / rsi_denom
        score = Decimal(
            str(round(min(1.0, 0.6 * min(1.0, dist_below_sma * 5) + 0.4 * rsi_strength), 4))
        )

        return [
            RawSignal(
                strategy_name=self.name,
                strategy_version=self.version,
                symbol=symbol,
                side="long",
                score=score,
                suggested_entry=entry,
                suggested_stop=stop,
                metadata={
                    "rsi": round(rsi_today, 4),
                    "rsi_slope": round(rsi_today - rsi_prev, 4),
                    "macd_histogram": round(hist_today, 6),
                    "macd_slope": round(hist_today - hist_prev, 6),
                    "adx": round(float(adx_v), 2),
                    "sma200": round(float(sma_trend), 4),
                    "dist_below_sma_pct": round(dist_below_sma * 100, 2),
                    "atr": round(float(atr_v), 4),
                },
            )
        ]

    # ------------------------------------------------------------------
    def exit_rules(
        self,
        position: PositionSnapshot,
        bars: pd.DataFrame,
        as_of: date,
    ) -> ExitDecision | None:
        """Exit policy: let winners ride.

        Two exits, in priority order:
          1. Hard stop — close ≤ entry − atr_stop_mult × ATR(14). Bounds the
             worst-case single-trade loss while we wait for MACD to roll
             over. Without this, a slow-grinding decline could compound
             losses well past the planned risk.
          2. MACD histogram ≤ 0 — momentum has turned over. Exit at next open.
        """
        view = self._slice(bars, as_of)
        # Need MACD slow EMA + signal smoothing for two readings of the histogram.
        min_hist = self.params.macd_slow_ma + self.params.macd_signal + self.params.atr_period + 5
        if len(view) < min_hist:
            return None

        close = view["close"]
        high = view["high"]
        low = view["low"]
        last_close = float(close.iloc[-1])
        avg_cost = float(position.avg_cost)

        # 1. Hard stop based on entry − N×ATR
        atr_v = atr(high, low, close, self.params.atr_period).iloc[-1]
        if not pd.isna(atr_v):
            stop_level = avg_cost - self.params.atr_stop_mult * float(atr_v)
            if last_close <= stop_level:
                return ExitDecision(reason="hard_stop", qty=position.qty)

        # 2. MACD histogram has rolled over (let winners ride until this fires).
        macd_df = compute_macd(
            close,
            self.params.macd_fast_ma,
            self.params.macd_slow_ma,
            self.params.macd_signal,
        )
        hist_today = float(macd_df["histogram"].iloc[-1])
        if not pd.isna(hist_today) and hist_today <= 0:
            return ExitDecision(reason="macd_below_zero", qty=position.qty)

        return None

    # ------------------------------------------------------------------
    # Market-cap filter — uses the latest fundamentals snapshot.
    # ------------------------------------------------------------------
    def _is_large_cap(self, symbol: str, as_of: date) -> bool:
        """Return True if `symbol`'s market cap is at or above the configured
        percentile (e.g., top 20% by default) of the S&P 500.

        Note: `as_of` is accepted for forward compatibility, but the current
        fundamentals_snapshot table holds the LATEST snapshot per symbol —
        not historical point-in-time. For backtests of past dates this means
        we apply today's market-cap ranks to historical bars, which is a
        small look-ahead bias on the universe filter only (not on prices).
        We accept that for v1; a true historical fundamentals table would
        be a Phase 5 enhancement.

        Returns False if there's no fundamentals row for the symbol — better
        to abstain than to incorrectly include / exclude.
        """
        pct = market_cap_percentile(symbol)
        if pct is None:
            return False
        return pct >= self.params.market_cap_pct_floor

    # ------------------------------------------------------------------
    @staticmethod
    def _slice(bars: pd.DataFrame, as_of: date) -> pd.DataFrame:
        """Return bars[index.date <= as_of] — enforces no-look-ahead."""
        idx_dates = bars.index.date if hasattr(bars.index, "date") else None
        if idx_dates is None:
            return bars
        mask = idx_dates <= as_of
        return bars[mask]

    @staticmethod
    def _symbol(view: pd.DataFrame) -> str:
        if "symbol" in view.columns:
            s = view["symbol"].iloc[-1]
            return str(s)
        return view.attrs.get("symbol", "UNKNOWN")
