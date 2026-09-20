"""52-week-high momentum — the book's one trend strategy.

  Eligible: adj close > SMA(200) and SMA(50) > SMA(200)   Stage-2 uptrend
            no single-day move beyond ±15% in the last 90 bars
                                                          gap screen (Alpha Architect)
            1-year realized vol ≤ 60%                     skip the wildest names
            close ≥ 90% of its 252-day high               George & Hwang (2004)
  Rank:     closeness to the 52-week high
            + Clenow slope quality (90-day log-price regression slope × R²)
            + residual tilt (12-month return minus the sector composite's)
            Highest score first; the position caps take the top of the list.
  Review:   new entries only on the weekly review day (Wednesday close,
            filled Thursday open) — momentum is a monthly-to-quarterly
            effect and daily re-ranking only adds turnover.
  Exit:     close ≤ entry × 0.85          15% stop (Han, Zhou & Zhu)
            close < SMA(100)              trend break (Clenow)
            close < 85% of 252-day high   fell out of the near-high set
  Sizing:   risk 0.75% of equity against the 15% stop (≈5% of equity per
            position), at most 10 open positions, and the regime layer's
            vol scalar shrinks size in high-vol markets — that is where
            momentum crashes (Daniel & Moskowitz 2016).

Knobs are the class constants below — edit and bump ``version``.
"""

from __future__ import annotations

import math
from datetime import date
from decimal import Decimal
from typing import ClassVar

import numpy as np
import pandas as pd

from stockscan.indicators import sector_relative_return, sma
from stockscan.strategies import ExitDecision, PositionSnapshot, RawSignal, Strategy


class Momentum52WeekHigh(Strategy):
    name = "momentum_52w_high"
    version = "2.0.0"
    display_name = "52-Week-High Momentum"
    description = (
        "Ranks stocks in a confirmed uptrend by how close they sit to their "
        "52-week high and how smooth the climb has been, buys the top of the "
        "list once a week, and holds until a 15% stop, a break of the 100-day "
        "average, or a 15% slide from the high."
    )
    tags = ("momentum", "trend_following", "long_only", "position")
    data_dependencies = ("sector_composites",)

    # ---- Sizing -----------------------------------------------------------
    default_risk_pct = 0.0075
    max_open_positions = 10
    sizes_down_in_high_vol = True

    # ---- Tunable knobs ----------------------------------------------------
    high_lookback: ClassVar[int] = 252
    min_closeness: ClassVar[float] = 0.90
    exit_closeness: ClassVar[float] = 0.85
    stop_pct: ClassVar[float] = 0.15
    trend_sma_period: ClassVar[int] = 200
    fast_sma_period: ClassVar[int] = 50
    exit_sma_period: ClassVar[int] = 100
    slope_window: ClassVar[int] = 90
    gap_lookback: ClassVar[int] = 90
    max_gap: ClassVar[float] = 0.15
    max_realized_vol: ClassVar[float] = 0.60
    residual_lookback: ClassVar[int] = 252
    residual_tilt_cap: ClassVar[float] = 0.25
    review_weekday: ClassVar[int] = 2  # Monday = 0

    manual = """\
## What this strategy is trying to do

Own the strongest stocks in the index while they stay strong. Stocks near
their 52-week high keep outperforming for months — the effect George and
Hwang documented in 2004 dominates plain past-return momentum in large caps
and, unlike it, does not reverse later. We rank the eligible names once a
week, buy the top of the list as position slots free up, and hold until the
trend breaks.

## The rules, one by one

**Uptrend.** Adjusted close above the 200-day SMA with the 50-day above
the 200-day. This keeps us out of names that are near a high only because
they collapsed a year ago.

**No recent blow-up.** Any single-day move beyond ±15% in the last 90 bars
disqualifies the name. Gaps that size mean an event, not a trend, and
Alpha Architect's screens drop them for the same reason.

**Not the wildest names.** One-year realized volatility above 60% is out.
Momentum's crashes concentrate in the highest-beta names.

**Near the high.** Close at least 90% of its 252-day high. That is the
entry gate; the ranking decides who among the eligible gets bought.

**The rank.** Three terms, each roughly 0–1, added:
- *closeness* — close ÷ 252-day high;
- *slope quality* — Clenow's 90-day regression of log price, annualized
  slope × R², squashed to 0–1, so steep *and* smooth beats steep and jagged;
- *residual tilt* — the stock's 12-month return minus its sector
  composite's, capped at ±25%, so we lean toward names climbing on their
  own merits rather than riding a sector wave (idiosyncratic momentum is the
  part that does not crash).

**Weekly review.** New entries only on Wednesday's close, filled Thursday's
open. Exits run every day.

**Exits.** A 15% stop from entry — the one stop with published evidence for
momentum, where it roughly doubles Sharpe by cutting crash months. A close
below the 100-day SMA, Clenow's trend-break exit. Or a close more than 15%
below the 52-week high, which means the name has left the set we buy.

## Sizing and the regime layer

Risk 0.75% of equity against the 15% stop, which works out to about 5% of
equity per position, at most ten positions. New entries stop while the
index is below its 200-day (the regime trend gate). In the top tercile of
market volatility the regime layer's vol scalar shrinks each new position —
momentum's worst months come in high-vol rebounds, and volatility scaling
is the best-evidenced fix.

## Sources

George & Hwang (2004); Jeon & Byun (2023) on the 52-week high and momentum
crashes; Clenow, *Stocks on the Move* (2015); Gray & Vogel, *Quantitative
Momentum*; Han, Zhou & Zhu on the 15% stop; Daniel & Moskowitz (2016) and
Barroso & Santa-Clara (2015) on volatility scaling; Blitz, Huij & Martens
(2011) on residual momentum.
"""

    def required_history(self) -> int:
        return max(self.high_lookback, self.trend_sma_period, self.residual_lookback) + 5

    # ------------------------------------------------------------------
    def signals(self, bars: pd.DataFrame, as_of: date) -> list[RawSignal]:
        if as_of.weekday() != self.review_weekday:
            return []
        view = bars[bars.index.date <= as_of]
        if len(view) < self.required_history():
            return []

        price = view["adj_close"].astype(float)
        sma_slow = sma(price, self.trend_sma_period).iloc[-1]
        sma_fast = sma(price, self.fast_sma_period).iloc[-1]
        if pd.isna(sma_slow) or pd.isna(sma_fast):
            return []
        if price.iloc[-1] <= sma_slow or sma_fast <= sma_slow:
            return []

        daily = price.pct_change().iloc[-self.gap_lookback :]
        if daily.abs().max() > self.max_gap:
            return []
        realized_vol = float(daily.std(ddof=0) * math.sqrt(252)) if len(daily) else 0.0
        if realized_vol > self.max_realized_vol:
            return []

        high_52w = float(price.iloc[-self.high_lookback :].max())
        closeness = float(price.iloc[-1]) / high_52w
        if closeness < self.min_closeness:
            return []

        slope_quality = self._slope_quality(price.iloc[-self.slope_window :])
        residual = sector_relative_return(view, as_of, lookback=self.residual_lookback)
        residual_tilt = (
            max(-self.residual_tilt_cap, min(self.residual_tilt_cap, residual))
            if residual is not None
            else 0.0
        )
        score = closeness + slope_quality + residual_tilt

        last_close = float(view["close"].iloc[-1])
        return [
            RawSignal(
                strategy_name=self.name,
                strategy_version=self.version,
                symbol=str(view.attrs.get("symbol", "UNKNOWN")),
                side="long",
                score=Decimal(str(round(score, 4))),
                suggested_entry=Decimal(str(round(last_close, 4))),
                suggested_stop=Decimal(str(round(last_close * (1.0 - self.stop_pct), 4))),
                metadata={
                    "closeness_52w": round(closeness, 4),
                    "slope_quality": round(slope_quality, 4),
                    "residual_return_12m": round(residual, 4) if residual is not None else None,
                    "residual_tilt": round(residual_tilt, 4),
                    "realized_vol_1y": round(realized_vol, 4),
                    "sma_50": round(float(sma_fast), 4),
                    "sma_200": round(float(sma_slow), 4),
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
        view = bars[bars.index.date <= as_of]
        if len(view) < self.high_lookback:
            return None
        last_close = float(view["close"].iloc[-1])
        if last_close <= float(position.avg_cost) * (1.0 - self.stop_pct):
            return ExitDecision(reason="stop_loss", qty=position.qty)

        price = view["adj_close"].astype(float)
        sma_exit = sma(price, self.exit_sma_period).iloc[-1]
        if not pd.isna(sma_exit) and price.iloc[-1] < sma_exit:
            return ExitDecision(reason="below_sma100", qty=position.qty)
        if price.iloc[-1] < self.exit_closeness * float(price.iloc[-self.high_lookback :].max()):
            return ExitDecision(reason="left_near_high_set", qty=position.qty)
        return None

    # ------------------------------------------------------------------
    @staticmethod
    def _slope_quality(closes: pd.Series) -> float:
        """Clenow's ranking metric: annualized regression slope of log price
        × R², squashed to (0, 1). A smooth 50%/yr climb lands near 0.85,
        flat near 0.5, a downtrend near 0."""
        log_p = np.log(closes.to_numpy(dtype=float))
        x = np.arange(len(log_p), dtype=float)
        slope, intercept = np.polyfit(x, log_p, 1)
        fitted = slope * x + intercept
        ss_res = float(np.sum((log_p - fitted) ** 2))
        ss_tot = float(np.sum((log_p - log_p.mean()) ** 2))
        r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
        annualized = slope * 252.0 * max(0.0, r2)
        return 1.0 / (1.0 + math.exp(-3.0 * annualized))
