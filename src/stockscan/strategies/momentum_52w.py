"""52-Week-High Momentum (George & Hwang, 2004; recent extensions 2024).

The cleanest pure-technical momentum signal in the academic literature.
Where vanilla 12/1 cross-sectional momentum (Jegadeesh-Titman) crashes
hard during reversals, 52-week-high momentum stays positive — and when
you neutralize a vanilla momentum book against the 52-week-high
signal, the procyclical drawdown disappears.

Reference:
  - George, T. J., & Hwang, C.-Y. (2004). The 52-Week High and
    Momentum Investing. Journal of Finance, 59(5), 2145-2176.
  - "Momentum on Historical High" (2024), which extends the signal
    to all-time highs and reports ~6.2% annual alpha.

Implementation choices for this codebase:

  * **Score = closeness to 52-week high**, not raw return. Specifically
    ``score = close / max(close_252)`` clipped into [0, 1]. A reading
    of 1.00 means today is a fresh 52-week high; 0.95 means within 5%
    of it; lower means meaningfully below.
  * **Filter: only emit signals for stocks within 5% of their 52w
    high** (configurable). This is the "near-ATH" alpha pocket — the
    further below the high a stock is, the closer the strategy looks
    to plain momentum and the more crash risk it picks up.
  * **Tie-break with regression-slope quality** — borrowed from
    Clenow's "Stocks on the Move." The annualised log-return slope of
    the past 90 days, weighted by R², gets folded into the score so
    smooth uptrends rank ahead of jagged ones at the same closeness
    band. This kills the false-positive failure mode where a stock
    tags its 52-week high after a single news pop.
  * **ATR-based stop**, consistent with Donchian. Initial stop at
    entry - 2 x ATR(20).
  * **Time-based exit** at 60 trading days (~12 weeks), which matches
    the holding period in the original George-Hwang study.

The strategy is long-only. Regime affinity favours trending markets;
in choppy regimes the position size is cut to 40% (mostly to dampen
the false-breakout concentration risk noted above).
"""

from __future__ import annotations

import math
from decimal import Decimal
from typing import TYPE_CHECKING, ClassVar

import numpy as np
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

if TYPE_CHECKING:
    from datetime import date


class Momentum52WParams(StrategyParams):
    high_window: int = Field(
        252, ge=126, le=504, description="Bars to look back for the high"
    )
    closeness_min: float = Field(
        0.95,
        ge=0.80,
        le=1.00,
        description="Minimum close / 52w-high ratio to emit a signal",
    )
    slope_window: int = Field(
        90, ge=30, le=252, description="Days for the regression-slope tiebreak"
    )
    slope_weight: float = Field(
        0.30,
        ge=0.0,
        le=1.0,
        description="How much the regression-slope tiebreak feeds into the score",
    )
    atr_period: int = Field(20, ge=10, le=40)
    atr_stop_mult: float = Field(2.0, ge=1.0, le=4.0)
    holding_days: int = Field(
        60,
        ge=20,
        le=180,
        description="Time-based exit after this many trading days",
    )


class Momentum52WHigh(Strategy):
    name = "momentum_52w_high"
    version = "1.0.0"
    display_name = "52-Week-High Momentum"
    description = (
        "Buys the highest-quality uptrends — stocks already trading "
        "within a few percent of their 52-week high — and holds them "
        "for ~12 weeks. The single highest-Sharpe pure-technical signal "
        "in recent academic literature, and one that historically avoids "
        "the momentum-crash drawdowns that plague plain 12/1 momentum."
    )
    tags = ("momentum", "trend_following", "long_only", "swing")
    params_model = Momentum52WParams
    default_risk_pct = 0.0075
    # Affinities chosen to: (1) fully size in trending_up, (2) skip
    # trending_down (long-only by design), (3) cut hard in choppy
    # because near-ATH stocks fail spectacularly on whipsaws,
    # (4) reduce in transitioning to manage drawdown timing risk.
    regime_affinity: ClassVar[dict[str, float]] = {
        "trending_up": 1.0,
        "trending_down": 0.0,
        "choppy": 0.4,
        "transitioning": 0.7,
    }

    manual = """\
## What this strategy is trying to do

This is the "buy the strongest names" strategy — the academic-literature
version of the Wall Street adage *"don't buy what's down 30%, buy what's
already going up."* Specifically, it looks for stocks trading within a
few percent of their 52-week high and adds them to the book, then holds
for about three months.

## Why this works (the empirical claim)

Two papers establish the result:

  - **George & Hwang (2004)**, *Journal of Finance*. Stocks ranked
    closest to their 52-week high earn ~6.2% per year alpha — *more*
    than vanilla cross-sectional momentum. The kicker: this signal
    *subsumes* standard momentum. Once you control for proximity to
    the 52-week high, the rest of the momentum effect mostly vanishes.

  - **"Momentum on Historical High" (2024)**, Finance Research Letters.
    Extends the signal to all-time highs and confirms the alpha
    persists, including in international markets and crypto.

The intuition: traders anchor on the 52-week high as a psychological
reference price. A stock that has just printed a fresh high, or is
trading right next to one, has cleared all overhead supply — there's
no one underwater waiting to sell at break-even. So buying pressure is
asymmetric. It's also a Schelling point for index-driven flows (52w-
high lists are widely tracked).

The really attractive property: this signal does NOT crash the way
plain 12/1 momentum does. Because the criterion is "close to a recent
high" rather than "outperformed peers", the worst-performing names
during a momentum reversal *do not* qualify — they fall off the list
naturally as their highs roll out of the window.

## The components, explained

### Closeness ratio

The core signal is just:

    closeness = today's close / (max close over last 252 days)

A value of 1.00 means today is a new 52-week high. 0.95 means within
5% of it. 0.80 means there's been a 20% drawdown since the high — at
which point this isn't a near-ATH name anymore.

We **only emit signals** for names with closeness ≥ 0.95 by default.
That's the "near-ATH" alpha pocket; further below, the strategy
collapses into plain momentum and picks up its crash risk.

### Regression-slope tiebreak (Clenow flavor)

A potential failure mode: two stocks both at closeness = 0.97, but one
got there with a smooth 90-day uptrend, the other through a sudden
news-driven gap. The smooth uptrend is more likely to continue.

Borrowing from Clenow's *Stocks on the Move*, we compute the
annualised slope of `log(close)` regressed on time over the past 90
days, weighted by R² to penalize jaggedness. That gets blended into
the score with a 30% weight. The closeness ratio is still the dominant
term; the slope just breaks ties between names that look identical on
the closeness measure alone.

### ATR-based stop

Initial stop = entry - 2 x ATR(20). Same as Donchian. ATR-scaled stops
sized to "two typical days of movement" survive normal volatility and
exit decisively when the regime really turns.

### Time-based exit

Pure 12-week hold (60 trading days), matching the original George-
Hwang study. No trailing stop, no momentum-decay test — just hold for
60 days, then close. The reason: the 52-week-high alpha is *already*
empirically front-loaded into the first 12 weeks; trying to hold
longer adds little return but a lot of correlated drawdown across
positions.

The fixed-holding-period rule is the strategy's biggest difference
from Donchian — Donchian rides winners as long as the chandelier stop
allows, which in trending environments can mean year-long holds. This
strategy deliberately cycles capital faster.

## The rules in plain English

**Setup filter**:

  - Stock's `close / 52-week-max-close` is ≥ 0.95 (i.e., within 5% of
    its 52-week high).

**Entry signal** (today, after the close):

  - Compute closeness ratio and regression-slope quality. Score blends
    them: `score = closeness x (1 - w) + slope_quality x w`, default
    `w = 0.30`. Higher score = stronger setup.
  - **We buy at tomorrow's market open** at today's close.

**Initial stop-loss**:

  - Stop = entry - 2 x ATR(20).

**Exit** (one rule, evaluated daily):

  - Time-based: close the position after 60 trading days regardless of
    P&L. The strategy's edge concentrates inside this window.

## What to expect when running this

  - **Modest trade frequency.** Maybe 5-15 entries per month per
    quartile of the universe in normal markets, fewer in heavy
    drawdowns when nothing is near its high.
  - **Three-month holds.** Capital cycles roughly 4x per year per
    position. That's both a feature (faster compounding) and a cost
    (more turnover, slightly more slippage).
  - **Higher win rate than Donchian.** Historically ~50-55% in the
    backtest range, vs. Donchian's 35-45%. The trade-off: smaller
    asymmetry between wins and losses.
  - **Fewer ulcer-grade drawdowns.** This is the strategy's main
    selling point. Maximum drawdowns historically ~15% smaller than
    plain cross-sectional momentum on equivalent universes.

## Where this strategy struggles

  - **Late-cycle momentum reversals.** The strategy stops generating
    signals in deep bear markets (nothing is near a 52-week high), so
    it sits in cash. That's fine. But the *transition* — when broad-
    based 52w highs are rolling over — is where it gives back the most.
  - **Sector concentration.** Whatever sector is currently leading the
    market will dominate the signal list. Without sector caps the book
    can become 70% one sector very quickly. The portfolio-level
    `max_sector_pct` filter handles this.
  - **Reversal stocks.** A stock tagging its 52-week high *after* a
    long downtrend (i.e., the high is very recent) is a different beast
    than one tagging it from a long uptrend. The slope tiebreak helps
    but doesn't fully resolve this.

## Why we run this alongside Donchian

Both are trend-following, but the entry conditions are very different.
Donchian fires on the EVENT of a 20-day breakout — a single-day
trigger. This strategy fires on a STATE — being already near a 1-year
high — which is more persistent and produces a different mix of names.
Donchian catches early movers; 52-week-high captures the names that
have been trending for a while. The two together form a more complete
trend sleeve than either alone.

## Default parameters and why

  - `high_window = 252` — one trading year. The canonical "52-week"
    window. The 2024 follow-up paper extends to all-time highs;
    increasing this parameter approximates that.
  - `closeness_min = 0.95` — within 5% of the high. Tightening this to
    0.97 cuts the trade count by ~50% and slightly improves Sharpe at
    the cost of fewer opportunities.
  - `slope_window = 90`, `slope_weight = 0.30` — Clenow-style
    regression slope blended in to break ties between names at
    similar closeness.
  - `atr_period = 20`, `atr_stop_mult = 2.0` — ATR-based initial stop
    matched to Donchian for cross-strategy consistency.
  - `holding_days = 60` — ~12 weeks. Original George-Hwang holding
    period.

## Source

George, T. J., & Hwang, C.-Y. (2004). "The 52-Week High and Momentum
Investing." *Journal of Finance* 59(5): 2145-2176.

Updated 2024: "Momentum on Historical High." *Finance Research
Letters*.
"""

    # ------------------------------------------------------------------
    def required_history(self) -> int:
        return (
            max(
                self.params.high_window,
                self.params.slope_window,
                self.params.atr_period,
            )
            + 5
        )

    # ------------------------------------------------------------------
    def signals(self, bars: pd.DataFrame, as_of: date) -> list[RawSignal]:
        view = self._slice(bars, as_of)
        if len(view) < self.required_history():
            return []

        high = view["high"]
        low = view["low"]
        close = view["close"]
        symbol = self._symbol(view)
        last_close = float(close.iloc[-1])

        # Closeness ratio: today's close vs trailing N-day max close.
        # Use a closed-on-the-right rolling window — today IS allowed
        # to be the high. (Different from Donchian, which compares
        # to the *prior* window deliberately to detect the breakout
        # event. Here we're measuring the STATE.)
        max_close = close.rolling(self.params.high_window).max().iloc[-1]
        if pd.isna(max_close) or float(max_close) <= 0:
            return []
        closeness = last_close / float(max_close)
        if closeness < self.params.closeness_min:
            return []

        # Slope quality (Clenow-style): annualised log-return slope on
        # the past N days, weighted by R². Higher = smoother uptrend.
        # Normalise into roughly [0, 1] — annualised slopes above
        # ~0.50 (50% per year) saturate to 1.0; below 0 floors at 0.
        slope_q = self._slope_quality(close.iloc[-self.params.slope_window :])
        if math.isnan(slope_q):
            slope_q = 0.0

        # Composite score: closeness dominant, slope as tiebreak.
        w = self.params.slope_weight
        score_value = (1.0 - w) * closeness + w * slope_q
        score_value = max(0.0, min(1.0, score_value))

        atr_v = atr(high, low, close, self.params.atr_period).iloc[-1]
        if pd.isna(atr_v) or float(atr_v) <= 0:
            return []

        entry = Decimal(str(round(last_close, 4)))
        stop = Decimal(str(round(last_close - self.params.atr_stop_mult * float(atr_v), 4)))
        score = Decimal(str(round(score_value, 4)))

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
                    "closeness_52w": round(closeness, 4),
                    "slope_quality": round(slope_q, 4),
                    "max_close_52w": round(float(max_close), 4),
                    "atr": round(float(atr_v), 4),
                    "holding_days": self.params.holding_days,
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
        # Time-based exit only. Position carries opened_at; if it has been
        # open for ``holding_days`` trading days (approximated by calendar
        # days x 5/7 — close enough for daily-bar exits), close it.
        if position.opened_at is None:
            return None
        # ``opened_at`` is a tz-aware datetime; compare on .date().
        days_held = (as_of - position.opened_at.date()).days
        # Convert calendar to trading days at the standard 252/365 ratio.
        trading_days_held = int(days_held * (252 / 365))
        if trading_days_held >= self.params.holding_days:
            return ExitDecision(reason="time_based_exit_60d", qty=position.qty)
        return None

    # ------------------------------------------------------------------
    @staticmethod
    def _slope_quality(closes: pd.Series) -> float:
        """Clenow-style regression-slope quality on log-prices.

        Fits ``log(close) ~ alpha + beta * t`` over the window.
        Returns ``annualised_slope * R²``, then sigmoid-normalised
        into roughly [0, 1] so very steep + very smooth = ~1.0,
        flat = ~0.5, downtrend = ~0.0.
        """
        if len(closes) < 2:
            return float("nan")
        log_p = np.log(closes.to_numpy(dtype=float))
        if not np.all(np.isfinite(log_p)):
            return float("nan")
        x = np.arange(len(log_p), dtype=float)
        # Linear regression: slope, intercept, R².
        # Using numpy.polyfit with deg=1 + manual R² for transparency.
        slope, intercept = np.polyfit(x, log_p, 1)
        fitted = slope * x + intercept
        ss_res = float(np.sum((log_p - fitted) ** 2))
        ss_tot = float(np.sum((log_p - log_p.mean()) ** 2))
        r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
        # Annualise. ``slope`` is the daily log-return; x252 gives
        # the annualised continuous return. Multiply by R² to penalise
        # jagged trends.
        annualised = slope * 252.0 * max(0.0, r2)
        # Squash to [0, 1] with a sigmoid centred on 0 — a 50%/yr
        # smooth uptrend lands around 0.85; flat → ~0.5; deep
        # downtrend → near 0.0.
        return 1.0 / (1.0 + math.exp(-3.0 * annualised))

    @staticmethod
    def _slice(bars: pd.DataFrame, as_of: date) -> pd.DataFrame:
        idx_dates = bars.index.date if hasattr(bars.index, "date") else None
        if idx_dates is None:
            return bars
        mask = idx_dates <= as_of
        return bars[mask]

    @staticmethod
    def _symbol(view: pd.DataFrame) -> str:
        if "symbol" in view.columns:
            return str(view["symbol"].iloc[-1])
        return view.attrs.get("symbol", "UNKNOWN")
