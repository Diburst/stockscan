"""Reversal Swing — trade tops & bottoms off the v2 reversal score.

Score-first strategy: it thresholds the signed v2 technical reversal score
(``stockscan.technical.score.compute_technical_score``, spec §6) rather than
computing its own indicators.

  Entry (long):  reversal score ≥ entry_threshold  → a confirmed bottom.
  Exit (first to fire):
      • reversal score ≤ −exit_threshold            → a confirmed top
      • close ≤ entry − atr_stop_mult × ATR(14)     → hard stop
      • held ≥ max_holding_days                      → time stop

Long-only (E*TRADE equities): a confirmed top is an *exit*, not a short. The
loop is buy the bottom → ride to the top → exit → re-enter the next bottom.

The reversal score already bundles the turn (reversal_trigger), the level
(pivot_proximity), relative strength (sector_rs), a reinforce-only trend tilt
(trend_location) and a climax volume multiplier (volume_confirm). This strategy
just turns "score ≥ threshold" into orders, so its entire edge lives in — and is
backtested through — that score on the same code path the live scanner uses.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import ClassVar

import pandas as pd
from pydantic import Field

from stockscan.indicators import atr
from stockscan.strategies import (
    ExitDecision,
    PositionSnapshot,
    RawSignal,
    Strategy,
    StrategyParams,
)


class ReversalSwingParams(StrategyParams):
    entry_threshold: float = Field(
        0.25, ge=0.05, le=0.95, description="Go long when the reversal score ≥ this (bottom)."
    )
    exit_threshold: float = Field(
        0.35, ge=0.05, le=0.95, description="Exit when the reversal score ≤ −this (top)."
    )
    atr_period: int = Field(14, ge=5, le=50)
    atr_stop_mult: float = Field(2.5, ge=1.0, le=5.0, description="Hard-stop ATR multiple.")
    max_holding_days: int = Field(20, ge=1, le=60, description="Time stop (swing horizon).")


class ReversalSwing(Strategy):
    name = "reversal_swing"
    version = "1.0.0"
    display_name = "Reversal Swing (tops & bottoms)"
    description = (
        "Buys confirmed bottoms and exits at confirmed tops using the signed v2 "
        "reversal score (turn + level + relative strength + trend tilt, scaled by "
        "a climax-volume multiplier). Long-only; a confirmed top is an exit."
    )
    tags = ("mean_reversion", "long_only", "swing")
    params_model = ReversalSwingParams
    default_risk_pct = 0.01
    # Reversals live in chop and in pullbacks within uptrends; counter-trend
    # bottoms (downtrends) are the falling-knife trades — taken, but down-weighted.
    regime_affinity: ClassVar[dict[str, float]] = {
        "trending_up": 1.0,
        "choppy": 1.0,
        "trending_down": 0.5,
        "transitioning": 0.7,
    }

    # ------------------------------------------------------------------
    def required_history(self) -> int:
        # Full reversal score wants trend_location's 200-day term (≥220 bars).
        return 230

    # ------------------------------------------------------------------
    def signals(self, bars: pd.DataFrame, as_of: date) -> list[RawSignal]:
        view = self._slice(bars, as_of)
        if len(view) < self.required_history():
            return []
        # Bound the indicator compute to the trailing window the score needs.
        # Trailing-window indicators give an identical last value on the tail as
        # on the full history, but rolling()/etc. then run over ~235 bars instead
        # of the whole (multi-thousand-bar) series — the per-call cost in the
        # backtest's inner loop.
        view = view.tail(self.required_history() + 5)

        score_obj = self._reversal_score(view, as_of)
        if score_obj is None:
            return []
        s = float(score_obj.score)
        if s < self.params.entry_threshold:
            return []  # not a confirmed bottom

        close = view["close"]
        atr_v = atr(view["high"], view["low"], close, self.params.atr_period).iloc[-1]
        if pd.isna(atr_v) or float(atr_v) <= 0:
            return []
        last_close = float(close.iloc[-1])
        entry = Decimal(str(round(last_close, 4)))
        stop = Decimal(str(round(last_close - self.params.atr_stop_mult * float(atr_v), 4)))

        # Setup type from the reinforce-only trend read, for sizing/management.
        trend = score_obj.breakdown.get("trend_location", {})
        trend_raw = float(trend.get("raw", trend.get("score", 0.0)))
        setup_type = "dip_in_uptrend" if trend_raw > 0 else "counter_trend_bottom"

        meta = score_obj.breakdown.get("_meta", {})
        return [
            RawSignal(
                strategy_name=self.name,
                strategy_version=self.version,
                symbol=self._symbol(view),
                side="long",
                score=Decimal(str(round(s, 4))),
                suggested_entry=entry,
                suggested_stop=stop,
                metadata={
                    "reversal_score": round(s, 4),
                    "D": meta.get("D"),
                    "C": meta.get("C"),
                    "atr": round(float(atr_v), 4),
                    "setup_type": setup_type,
                    "reversal_trigger": score_obj.breakdown.get("reversal_trigger", {}).get("score"),
                    "pivot_proximity": score_obj.breakdown.get("pivot_proximity", {}).get("score"),
                    "trend_raw": round(trend_raw, 4),
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
        view = self._slice(bars, as_of)
        if view.empty:
            return None

        # Time stop — always checkable (uses the full holding history).
        opened_d = position.opened_at.date()
        bars_held = int((view.index.date > opened_d).sum())
        if bars_held >= self.params.max_holding_days:
            return ExitDecision(reason="time_stop", qty=position.qty)

        # Bound the indicator compute (same trailing-window argument as signals()).
        view = view.tail(self.required_history() + 5)
        close = view["close"]
        last_close = float(close.iloc[-1])

        # Hard stop on entry − ATR multiple (degrades gracefully without full history).
        if len(view) >= self.params.atr_period + 1:
            atr_v = atr(view["high"], view["low"], close, self.params.atr_period).iloc[-1]
            if not pd.isna(atr_v):
                stop_level = float(position.avg_cost) - self.params.atr_stop_mult * float(atr_v)
                if last_close <= stop_level:
                    return ExitDecision(reason="hard_stop", qty=position.qty)

        # Reversal-top exit: score flipped to a confirmed top.
        score_obj = self._reversal_score(view, as_of)
        if score_obj is not None and float(score_obj.score) <= -self.params.exit_threshold:
            return ExitDecision(reason="reversal_top", qty=position.qty)

        return None

    # ------------------------------------------------------------------
    def _reversal_score(self, view: pd.DataFrame, as_of: date):
        """Compute the signed v2 reversal score for this symbol, or None.

        Lazy import keeps this leaf strategy free of the technical layer at
        module-load time (mirrors donchian's get_bars import)."""
        try:
            from stockscan.technical.score import compute_technical_score
        except Exception:
            return None
        try:
            return compute_technical_score(type(self), view, as_of)
        except Exception:
            return None

    # ------------------------------------------------------------------
    @staticmethod
    def _slice(bars: pd.DataFrame, as_of: date) -> pd.DataFrame:
        """Return bars[index.date <= as_of] — enforces no-look-ahead."""
        if not hasattr(bars.index, "date"):
            return bars
        # Fast path: the index is sorted ascending, so if the last bar is already
        # ≤ as_of there are no future bars to mask off — skip the O(n) boolean
        # mask + date-object array (the backtest already hands us a sliced frame).
        if len(bars) and bars.index[-1].date() <= as_of:
            return bars
        out = bars[bars.index.date <= as_of]
        # Preserve the symbol for sector_rs (boolean indexing can drop attrs).
        if "symbol" in bars.columns and len(out):
            out.attrs["symbol"] = str(out["symbol"].iloc[-1])
        elif bars.attrs.get("symbol"):
            out.attrs["symbol"] = bars.attrs["symbol"]
        return out

    @staticmethod
    def _symbol(view: pd.DataFrame) -> str:
        if "symbol" in view.columns and len(view):
            return str(view["symbol"].iloc[-1])
        return str(view.attrs.get("symbol", "UNKNOWN"))
