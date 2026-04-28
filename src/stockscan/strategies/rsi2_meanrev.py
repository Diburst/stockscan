"""RSI(2) Mean-Reversion (Connors).

Reference implementation per DESIGN §6.1.

  Setup:    Close > SMA(200)         (only buy in long-term uptrends)
  Entry:    RSI(2) < threshold (10)   → buy at next open
  Exit (whichever first):
            Close > SMA(5)            → sell at next open
            Close < entry − 2.5×ATR   → hard stop at next open
            Held > max_holding_days   → time stop

The class subclasses Strategy, which auto-registers via __init_subclass__.
The contract tests in tests/test_strategy_contract.py run against this
strategy automatically.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pandas as pd
from pydantic import Field

from stockscan.indicators import atr, rsi, sma
from stockscan.strategies import (
    ExitDecision,
    PositionSnapshot,
    RawSignal,
    Strategy,
    StrategyParams,
)


class RSI2Params(StrategyParams):
    """Tunable parameters for RSI(2) Mean-Reversion."""

    rsi_period: int = Field(2, ge=1, le=20, description="RSI lookback window")
    rsi_threshold: float = Field(
        10.0, ge=1.0, le=30.0, description="Enter when RSI ≤ this"
    )
    trend_sma_period: int = Field(
        200, ge=20, le=400, description="Long-term uptrend filter"
    )
    exit_sma_period: int = Field(
        5, ge=2, le=20, description="Mean-reversion exit when close > this SMA"
    )
    atr_period: int = Field(14, ge=5, le=50)
    atr_stop_mult: float = Field(2.5, ge=1.0, le=5.0, description="Hard-stop ATR multiple")
    max_holding_days: int = Field(10, ge=1, le=30)


class RSI2MeanReversion(Strategy):
    name = "rsi2_meanrev"
    version = "1.0.0"
    display_name = "RSI(2) Mean-Reversion"
    description = (
        "A short-term mean-reversion strategy that buys stocks during brief "
        "pullbacks inside an established uptrend. Holds for a few days and exits "
        "when the stock recovers back to its short-term average — or when a "
        "pre-set stop or time limit is hit."
    )
    tags = ("mean_reversion", "long_only", "swing")
    params_model = RSI2Params
    default_risk_pct = 0.01

    manual = """\
## What this strategy is trying to do

This is a "buy the dip in an uptrend" strategy. It looks for stocks that are
in a long-term uptrend but have just had a short, sharp drop, and assumes the
drop is temporary. We buy when the stock looks oversold, hold for a few days,
and sell as soon as it recovers to its short-term average price.

The intuition: in healthy bull-market conditions, brief pullbacks in
otherwise-rising stocks tend to recover quickly. We're betting on that
recovery — many small, frequent wins rather than a few big ones.

## The components, explained

This strategy uses four common technical indicators. None of them are
mysterious; they're all simple math on recent prices.

### RSI — Relative Strength Index

A momentum indicator that ranges from 0 to 100. Originally invented by J.
Welles Wilder Jr. in 1978. It compares the size of recent up-moves to the
size of recent down-moves over a lookback window:

  - **RSI near 100** = the stock has been going up almost every day in the
    window. It's "overbought" — looks extended to the upside.
  - **RSI near 0** = the stock has been going down almost every day. It's
    "oversold" — looks extended to the downside.
  - **RSI near 50** = up-moves and down-moves have been roughly balanced.

The "(2)" in **RSI(2)** means we use a **2-day lookback window** instead of
the more common 14-day default. Two days makes the indicator very twitchy —
RSI(2) can swing from 90 down to 5 in a couple of bars. That sensitivity is
exactly what we want for catching short, sharp pullbacks.

We enter when RSI(2) drops below 10, meaning: the stock has dropped hard
over just the last day or two.

### SMA — Simple Moving Average

The average closing price over the last N days. Plotted on a chart, it's a
smooth line that lags behind the actual price.

We use two SMAs in this strategy:

  - **SMA(200)** — the 200-day simple moving average. A standard proxy for
    the long-term trend. If today's close is above the 200-day SMA, the
    stock is generally considered to be in a long-term uptrend.
  - **SMA(5)** — the 5-day simple moving average. Used as a short-term
    "fair value" reference for the exit. When the price climbs back above
    the 5-day SMA, we say it has "mean-reverted" and we sell.

### ATR — Average True Range

A volatility measurement, also from J. Welles Wilder. It tells you the
typical daily price range of a stock over a lookback window — not which
direction it's moving, just how much it's moving.

A stock with ATR = $2 typically moves about $2 between its daily low and
high. A stock with ATR = $10 typically moves $10. Two stocks at the same
price can have wildly different ATRs — high-volatility names like NVDA
move far more per day than low-volatility names like KO.

We use ATR(14) — the average daily range over the last 14 trading days —
to size our hard stop-loss. A 2.5×ATR stop on a high-volatility stock will
be much wider (in dollars) than the same multiple on a low-vol name. That's
correct: tighter stops on quiet stocks, looser stops on noisy ones.

## The rules in plain English

**Setup filter** (only consider stocks that pass this):
  - Today's close is above the 200-day SMA.
  - Translation: only buy stocks in long-term uptrends. We don't try to catch
    falling knives.

**Entry signal** (today, after the close):
  - RSI(2) has dropped below 10.
  - Translation: the stock has just had a sharp 1–2 day pullback.
  - **We buy at tomorrow's market open.**

**Exits** (whichever happens first; checked daily after the close):
  1. **Mean reversion exit**: today's close is back above the 5-day SMA.
     Translation: the stock has recovered. Sell tomorrow at the open.
  2. **Hard stop-loss**: today's close is below `entry_price - 2.5 × ATR(14)`.
     Translation: the trade has gone significantly against us. Sell tomorrow
     at the open before more damage.
  3. **Time stop**: we've held for 10 trading days without either exit
     triggering. The thesis (quick recovery) didn't play out — sell.

## What to expect when running this

  - **Lots of small trades.** A few dozen per year per name in active markets.
  - **Short holding period.** Average ~3 trading days; rarely the full 10.
  - **High win rate.** Historically around 60–70% of trades close at a
    profit, though wins are small (typically 1–3%).
  - **Smaller losses.** When the stop hits it usually does so cleanly,
    around 2–4% loss.
  - **Edge comes from frequency, not size.** The strategy doesn't make money
    by hitting home runs — it makes money by hitting lots of singles.

## Where this strategy struggles

  - **Strong sustained downtrends** (the SMA(200) filter catches most of
    these — but the filter only updates once a day, and a stock can fall
    below its 200-day average mid-trade).
  - **Earnings-driven gaps**. A negative earnings surprise overnight can
    blow right through the stop. We filter signals where earnings are
    within 5 trading days, but unscheduled news can still hit.
  - **Choppy, range-bound markets** with no clear long-term trend can
    starve the strategy of qualifying setups.

## Default parameters and why

  - `rsi_period = 2` — the canonical Connors setting; very responsive.
  - `rsi_threshold = 10` — only enter when truly oversold. Looser thresholds
    (e.g., 30) trade more often but with weaker edge per trade.
  - `trend_sma_period = 200` — standard long-term trend filter.
  - `exit_sma_period = 5` — short enough to catch quick reversions; longer
    settings turn this into a trend-following strategy by accident.
  - `atr_stop_mult = 2.5` — wide enough to absorb normal volatility,
    tight enough to limit damage when the trade fails.
  - `max_holding_days = 10` — the original Connors rule.

## Source

Larry Connors and Cesar Alvarez, *Short Term Trading Strategies That Work*
(2008). The book contains backtests of this and several similar strategies.
"""

    # ------------------------------------------------------------------
    def required_history(self) -> int:
        # SMA(200) needs 200; ATR(14) needs ~15 of warmup. Add buffer.
        return self.params.trend_sma_period + 20

    # ------------------------------------------------------------------
    def signals(self, bars: pd.DataFrame, as_of: date) -> list[RawSignal]:
        view = self._slice(bars, as_of)
        if len(view) < self.required_history():
            return []

        close = view["close"]
        rsi_v = rsi(close, self.params.rsi_period).iloc[-1]
        sma_trend = sma(close, self.params.trend_sma_period).iloc[-1]
        atr_v = atr(view["high"], view["low"], close, self.params.atr_period).iloc[-1]
        last_close = close.iloc[-1]
        symbol = self._symbol(view)

        if pd.isna(rsi_v) or pd.isna(sma_trend) or pd.isna(atr_v):
            return []
        if last_close <= sma_trend:
            return []
        if rsi_v >= self.params.rsi_threshold:
            return []

        entry = Decimal(str(round(float(last_close), 4)))
        stop = Decimal(str(round(float(last_close - self.params.atr_stop_mult * atr_v), 4)))
        # Lower RSI = stronger signal; map to a [0, 1] score.
        score = Decimal(str(round(1.0 - float(rsi_v) / self.params.rsi_threshold, 4)))

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
                    "rsi": round(float(rsi_v), 4),
                    "atr": round(float(atr_v), 4),
                    "sma_trend": round(float(sma_trend), 4),
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
        if len(view) < max(self.params.exit_sma_period, self.params.atr_period) + 1:
            return None

        close = view["close"]
        last_close = float(close.iloc[-1])
        sma_exit = sma(close, self.params.exit_sma_period).iloc[-1]
        atr_v = atr(view["high"], view["low"], close, self.params.atr_period).iloc[-1]

        # Time stop
        opened_d = position.opened_at.date()
        bars_held = (view.index.date > opened_d).sum()
        if bars_held >= self.params.max_holding_days:
            return ExitDecision(reason="time_stop", qty=position.qty)

        # Mean-reversion exit
        if not pd.isna(sma_exit) and last_close > float(sma_exit):
            return ExitDecision(reason="mean_reverted_above_sma5", qty=position.qty)

        # Hard stop based on entry × ATR multiple
        avg_cost = float(position.avg_cost)
        if not pd.isna(atr_v):
            stop_level = avg_cost - self.params.atr_stop_mult * float(atr_v)
            if last_close <= stop_level:
                return ExitDecision(reason="hard_stop", qty=position.qty)

        return None

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
