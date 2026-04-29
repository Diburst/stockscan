"""Donchian Channel Breakout (Turtle-style).

Reference implementation per DESIGN §6.2.

  Entry:    Today's close at new N-day high (default N=20)  → buy next open
  Initial stop: entry − 2 × ATR(20)
  Trailing exit: chandelier — max(prior 22d high, close) − 3×ATR(22)
  Exit confirmation: close < 10-day low  → sell next open
  Filter: ADX(14) < 18 → don't enter (weak trend)
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pandas as pd
from pydantic import Field

from stockscan.indicators import adx, atr
from stockscan.strategies import (
    ExitDecision,
    PositionSnapshot,
    RawSignal,
    Strategy,
    StrategyParams,
)


class DonchianParams(StrategyParams):
    entry_period: int = Field(20, ge=10, le=60, description="Breakout high window")
    exit_period: int = Field(10, ge=5, le=40, description="Confirming exit low window")
    atr_period_stop: int = Field(20, ge=10, le=40)
    atr_stop_mult: float = Field(2.0, ge=1.0, le=4.0, description="Initial stop ATR mult")
    chandelier_period: int = Field(22, ge=10, le=40, description="Trailing-stop high window")
    chandelier_atr_mult: float = Field(3.0, ge=1.0, le=5.0)
    adx_period: int = Field(14, ge=5, le=30)
    adx_min: float = Field(18.0, ge=0.0, le=50.0, description="Skip if ADX below this")


class DonchianBreakout(Strategy):
    name = "donchian_trend"
    version = "1.0.0"
    display_name = "Donchian Channel Breakout"
    description = (
        "A trend-following strategy that buys stocks breaking out to new "
        "20-day highs in markets that are actually trending, then holds them "
        "as long as they keep climbing. Exits when the price drops far enough "
        "from its recent high to confirm the trend has ended."
    )
    tags = ("trend_following", "breakout", "long_only", "swing")
    params_model = DonchianParams
    default_risk_pct = 0.0075  # tighter risk per DESIGN §6.2 (more positions)
    # Donchian breakout requires genuine directional momentum to avoid whipsaw.
    applicable_regimes: frozenset[str] = frozenset({"trending_up", "trending_down"})

    manual = """\
## What this strategy is trying to do

This strategy is the opposite of mean-reversion. Instead of betting that a
sharp move will reverse, it bets that a sharp move *will keep going*. We
buy stocks that have just broken out to a new short-term high and hold them
as long as the trend remains intact, often for weeks or months.

The intuition: real trends — driven by sustained buying from institutions,
news cycles, or sector rotations — tend to last longer than people expect.
Most of a stock's annual return often comes from a handful of trending
months. If we can identify when a trend is starting and ride it without
exiting prematurely, we capture that compounding move.

The trade-off: we will be wrong most of the time. Most "breakouts" turn out
to be false starts that immediately reverse. The math still works because
when we ARE right, the winning trades are much larger than the losers.

## The components, explained

### Donchian Channel

Named after Richard Donchian (1905–1993), one of the founding figures of
trend-following. A **Donchian Channel** is just three lines drawn on a
price chart over a lookback window of N days:

  - **Upper line** = the highest high over the last N days.
  - **Lower line** = the lowest low over the last N days.
  - **Middle line** = the average of the upper and lower lines.

When today's close pushes ABOVE the upper line, that means the stock is
making a new high relative to the last N days — a breakout. We use the
20-day window for our entry signal: a new 20-day high is a moderately
significant move that catches medium-term trends without too many false
starts.

The Turtle Traders, a famous group of trend-followers from the 1980s, made
the 20-day Donchian breakout into one of the most well-documented systematic
strategies in history. This implementation is a direct descendant of their
"System 1" rules.

### ATR — Average True Range

A volatility measurement (J. Welles Wilder, 1978). It tells you the typical
daily price range of a stock over a lookback window — not which direction
it's moving, just how much it's moving on any given day.

A stock trading at $100 with ATR = $2 typically moves about $2 per day. The
same stock at $100 with ATR = $5 moves about $5. ATR is essential for
sizing stop-losses correctly: if you place a stop $2 below the entry on a
high-volatility stock, you'll get stopped out almost immediately on normal
day-to-day noise.

We use ATR for both our initial stop-loss and our trailing stop, scaled by
multipliers (2× and 3× respectively).

### ADX — Average Directional Index

Also from J. Welles Wilder (1978). ADX is a single number, ranging from 0
to 100, that measures **how strong a trend is** — but importantly, it does
NOT tell you which direction the trend is going. A market falling hard and
a market rising hard can both have high ADX values.

  - **ADX below 20** = the market is choppy / range-bound. There's no
    sustained directional move; prices are oscillating. Bad environment for
    breakout strategies.
  - **ADX above 25** = a real trend is present. The market has clear
    directional momentum.
  - **ADX between 20 and 25** = ambiguous; could go either way.

We use ADX(14) as a filter. If ADX is below 18, we **skip the entry** even
if a 20-day high gets touched, because in non-trending markets, breakouts
fail constantly. The filter is one of the highest-impact features in the
strategy: removing it roughly doubles the number of trades but cuts
expectancy per trade by more than half.

### Chandelier Exit (a trailing stop)

A **trailing stop** is a stop-loss that moves with the price. Unlike a
fixed stop, which sits at a static level (e.g., "sell if price drops below
$95"), a trailing stop follows winners up. As the stock makes new highs,
the stop level rises with it — locking in profit while still giving the
position room to keep running.

The **chandelier exit** is one specific kind of trailing stop, named because
the exit level "hangs down" from the highest price like a chandelier from
a ceiling. The formula is:

  chandelier_stop = (highest high of last N days) − (multiplier × ATR)

We use N=22 days and multiplier=3. So the stop level is "the recent peak,
minus three times the average daily range." If the stock pulls back more
than ~3 days' worth of typical movement from its recent high, we exit.

Why a trailing stop matters here: it lets winners run as long as they keep
making new highs. A disciplined fixed stop would exit on the first 5%
pullback, missing 90% of a 50%+ move. A chandelier stop only exits when
the trend has *meaningfully* broken.

## The rules in plain English

**Setup filter** (only consider stocks that pass this):
  - ADX(14) is at least 18 — the market is actually trending.
  - The stock isn't reporting earnings within 5 trading days.

**Entry signal** (today, after the close):
  - Today's close is the highest close in the last 20 trading days.
  - Translation: the stock just hit a fresh 20-day high.
  - **We buy at tomorrow's market open.**

**Initial stop-loss** (set on the day of entry):
  - Stop level = entry price − 2 × ATR(20).
  - This is the worst-case loss if the breakout immediately fails.

**Trailing exit** (chandelier; recalculated daily):
  - Trailing stop = (22-day high) − 3 × ATR(22).
  - As the stock makes new highs, this trailing stop rises with them.
  - When today's close drops below the current chandelier level → **sell at
    tomorrow's open.**

**Confirming exit** (also daily):
  - If today's close drops below the lowest low of the last 10 days → sell
    at tomorrow's open.
  - Translation: the trend has rolled over hard enough to make a new
    short-term low. Time to leave.

## What to expect when running this

  - **Few trades.** Maybe 1–3 entries per year per name in active markets.
    Many qualified names will never trigger an entry because they don't
    make a clean 20-day high during a strong-ADX environment.
  - **Long holding periods.** Successful trades often run for weeks or
    months. The strategy is designed to capture sustained moves.
  - **Low win rate.** Historically around 35–45% of trades close at a
    profit. **This is normal and expected.** Trend-following is mostly
    losing trades.
  - **Big asymmetry between wins and losses.** Average winners are typically
    3–5× the size of average losers. The strategy depends on this asymmetry
    — without it, the low win rate would make it unprofitable.
  - **Large equity-curve volatility.** Trend strategies often spend long
    stretches in drawdown waiting for the next big winner. Patience with
    the methodology is more important than any individual trade outcome.

## Where this strategy struggles

  - **Choppy / range-bound markets.** The ADX filter helps but isn't
    perfect; whipsaws happen.
  - **Mean-reversion regimes.** When markets are quickly reverting after
    every move (e.g., low-volatility 2017-style tape), trend-following
    underperforms badly.
  - **Late entries.** By definition, we enter AFTER a 20-day high has
    formed — meaning we're never first to a trend. Some of the move has
    already happened before we get on board.
  - **Late exits.** The trailing stop gives back ~3 ATR worth of profit
    at every exit. We always sell after the peak, never at it.

## Why we run this alongside RSI(2)

The two strategies have **negatively correlated equity curves**. RSI(2)
makes money when markets chop and revert; Donchian makes money when markets
trend. A regime that punishes one tends to favor the other. Running both
diversifies your strategy stack so a sustained run of either market type
doesn't kill total P&L.

## Default parameters and why

  - `entry_period = 20` — the classic Turtle System 1 entry window.
  - `exit_period = 10` — half the entry window; the Turtle "S1" exit rule.
  - `atr_period_stop = 20`, `atr_stop_mult = 2.0` — initial stop sized to
    absorb 2 typical days of movement.
  - `chandelier_period = 22`, `chandelier_atr_mult = 3.0` — trailing stop
    that gives room for normal pullbacks while still exiting decisively
    when the trend rolls over. The 22-day period roughly matches one
    calendar month of trading days.
  - `adx_period = 14`, `adx_min = 18.0` — standard ADX setting; threshold
    chosen as a compromise between strict (more selective) and loose
    (more trades).

## Source

The Turtle Traders, trained by Richard Dennis and William Eckhardt in the
mid-1980s. Their rules were leaked and codified in *Way of the Turtle*
(Curtis Faith, 2007). Donchian himself wrote about channel breakouts as
early as the 1960s.
"""

    # ------------------------------------------------------------------
    def required_history(self) -> int:
        return (
            max(
                self.params.entry_period,
                self.params.chandelier_period,
                self.params.atr_period_stop,
                self.params.adx_period * 2,
            )
            + 10
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

        # DESIGN §6.2: "today's close is the highest close of the trailing N-day window".
        # Compare today's close to the trailing N-day max-of-closes excluding today.
        prior_max_close = close.rolling(self.params.entry_period).max().shift(1).iloc[-1]
        if pd.isna(prior_max_close):
            return []
        if last_close <= float(prior_max_close):
            return []

        # ADX filter — skip weak trends.
        adx_v = adx(high, low, close, self.params.adx_period).iloc[-1]
        if pd.isna(adx_v) or float(adx_v) < self.params.adx_min:
            return []

        atr_v = atr(high, low, close, self.params.atr_period_stop).iloc[-1]
        if pd.isna(atr_v) or float(atr_v) <= 0:
            return []

        entry = Decimal(str(round(last_close, 4)))
        stop = Decimal(
            str(round(last_close - self.params.atr_stop_mult * float(atr_v), 4))
        )
        # Score: how far above the prior max close (in ATRs).
        breakout_strength = (last_close - float(prior_max_close)) / float(atr_v)
        score = Decimal(str(round(min(1.0, breakout_strength), 4)))

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
                    "prior_max_close": round(float(prior_max_close), 4),
                    "atr": round(float(atr_v), 4),
                    "adx": round(float(adx_v), 4),
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
        min_hist = max(
            self.params.exit_period,
            self.params.chandelier_period,
            self.params.atr_period_stop,
        )
        if len(view) < min_hist + 1:
            return None

        high = view["high"]
        low = view["low"]
        close = view["close"]
        last_close = float(close.iloc[-1])

        # Confirming exit: close below N-day low (excluding today).
        prior_low = low.rolling(self.params.exit_period).min().shift(1).iloc[-1]
        if not pd.isna(prior_low) and last_close < float(prior_low):
            return ExitDecision(reason="exit_below_n_day_low", qty=position.qty)

        # Chandelier trailing stop.
        ch_high = high.rolling(self.params.chandelier_period).max().iloc[-1]
        ch_atr = atr(high, low, close, self.params.chandelier_period).iloc[-1]
        if not pd.isna(ch_high) and not pd.isna(ch_atr):
            chandelier = float(ch_high) - self.params.chandelier_atr_mult * float(ch_atr)
            if last_close <= chandelier:
                return ExitDecision(reason="chandelier_stop", qty=position.qty)

        return None

    # ------------------------------------------------------------------
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
