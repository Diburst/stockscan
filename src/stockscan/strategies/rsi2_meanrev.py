"""RSI(2) pullback in an uptrend — the book's one mean-reversion strategy.

  Setup:   adj close > SMA(200)                 uptrend only (Connors; Alvarez)
           sector 1-month return > −5%          the dip is the stock's, not
                                                 the sector's (Da-Liu-Schaumburg)
           selloff volume < 1.5× normal         high-turnover drops continue
                                                 (Medhat-Schmeling 2022)
  Entry:   RSI(2) < 10                          → buy at next open
  Rank:    idiosyncratic drop = sector 1-month return − stock 1-month return
           (most stock-specific drop first)
  Exit:    close > SMA(5)  or  RSI(2) > 50      → sell at next open
           held 10 bars                         → time stop
  Stop:    none. Price stops cut this trade's returns far more than its
           drawdown (Kaminski & Lo 2014; Connors; Alvarez), so risk is
           carried by the fixed 10% position size, the position cap, the
           time stop and the regime layer's trend gate.

The regime layer's vol scalar does not apply: reversal profits rise with
volatility (Nagel 2012), so this strategy trades full size in high vol.

Knobs are the class constants below — edit and bump ``version``.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import ClassVar

import pandas as pd

from stockscan.indicators import rsi, sector_relative_return, sector_return, sma
from stockscan.strategies import ExitDecision, PositionSnapshot, RawSignal, Strategy


class RSI2MeanReversion(Strategy):
    name = "rsi2_meanrev"
    version = "2.0.0"
    display_name = "RSI(2) Pullback"
    description = (
        "Buys a stock in a long-term uptrend right after a sharp two-day drop "
        "that is specific to the stock rather than its sector, holds a few days, "
        "and sells on the first recovery. No price stop — a time stop and small "
        "fixed positions carry the risk."
    )
    tags = ("mean_reversion", "long_only", "swing")
    data_dependencies = ("sector_composites",)

    # ---- Sizing -----------------------------------------------------------
    position_pct = 0.10  # fixed fraction of equity per position; no stop
    sizes_down_in_high_vol = False

    # ---- Tunable knobs ----------------------------------------------------
    rsi_period: ClassVar[int] = 2
    rsi_entry: ClassVar[float] = 10.0
    rsi_exit: ClassVar[float] = 50.0
    trend_sma_period: ClassVar[int] = 200
    exit_sma_period: ClassVar[int] = 5
    max_holding_bars: ClassVar[int] = 10
    # Sector-relative conditioning: 21 bars ≈ one month.
    relative_lookback: ClassVar[int] = 21
    sector_min_return: ClassVar[float] = -0.05
    # Selloff volume: mean volume over the last ``selloff_bars`` vs the
    # ``volume_baseline`` mean before them.
    selloff_bars: ClassVar[int] = 2
    volume_baseline: ClassVar[int] = 50
    max_relative_volume: ClassVar[float] = 1.5
    # Optional one-bar confirmation: require today's close above yesterday's
    # (the "hook") so we buy the first up-close rather than the falling knife.
    require_hook: ClassVar[bool] = False

    manual = """\
## What this strategy is trying to do

Buy the dip — but only a particular kind of dip. The stock must be in a
long-term uptrend, it must have just dropped hard over one or two days, and
the drop must be the stock's own rather than its whole sector falling. We
hold for a few days and sell on the first sign of recovery.

The bet is that, in healthy names, short sharp pullbacks recover quickly.
It is a high-win-rate, small-average-trade strategy; the edge per trade is
a few tenths of a percent, so costs matter and turnover is kept low by the
10-bar time stop.

## The rules, one by one

**Uptrend.** Adjusted close above the 200-day simple moving average. This is
the Connors / Alvarez setup and the only regime control at the stock level.

**Oversold.** RSI(2) below 10. A two-day RSI is deliberately twitchy: it
reads "the stock fell hard over the last two sessions", nothing more.

**The dip is idiosyncratic.** We compare the stock's one-month return with
its equal-weight sector composite's. If the *sector* is down more than 5%
over the month we stand aside — a sector in a downtrend keeps falling, and
the literature finds that short-term reversal survives in large caps only in
its within-sector form. Among the names that pass, we rank by how much more
the stock fell than its sector: the most stock-specific drop goes first.

**Quiet selloff.** If the two-day selloff ran on more than 1.5× the stock's
normal volume we skip it. Heavy-volume declines tend to continue; the dips
that revert are the ones nobody was rushing to sell into.

**Exit.** Close back above the 5-day SMA, or RSI(2) back above 50, whichever
comes first; otherwise out after 10 bars. There is no price stop. Every
study of this trade that adds one finds it lowers returns more than it
lowers drawdown, because the stop sells exactly the extreme the strategy is
built to buy.

## Sizing and the regime layer

Each position is 10% of equity — a fixed fraction, since with no stop there
is no "risk per share" to size from. The portfolio cap and the regime
layer's trend gate (no new entries while the index is below its 200-day)
bound the aggregate risk. The vol scalar does not apply to this strategy:
mean-reversion returns are historically highest in high-volatility markets.

## Sources

Connors & Alvarez, *Short Term Trading Strategies That Work* (2008); Cesar
Alvarez, alvarezquanttrading.com (2015–2024); Da, Liu & Schaumburg (2014)
on industry-residual reversal; Medhat & Schmeling (2022) on turnover;
Kaminski & Lo (2014) on stop-loss rules; Nagel (2012) on reversal and VIX.
"""

    def required_history(self) -> int:
        return self.trend_sma_period + self.volume_baseline + self.selloff_bars

    # ------------------------------------------------------------------
    def signals(self, bars: pd.DataFrame, as_of: date) -> list[RawSignal]:
        view = bars[bars.index.date <= as_of]
        if len(view) < self.required_history():
            return []

        price = view["adj_close"].astype(float)
        rsi_now = rsi(price, self.rsi_period).iloc[-1]
        trend = sma(price, self.trend_sma_period).iloc[-1]
        if pd.isna(rsi_now) or pd.isna(trend):
            return []
        if price.iloc[-1] <= trend:
            return []
        if rsi_now >= self.rsi_entry:
            return []
        if self.require_hook and price.iloc[-1] <= price.iloc[-2]:
            return []

        sector_1m = sector_return(view, as_of, lookback=self.relative_lookback)
        relative_1m = sector_relative_return(view, as_of, lookback=self.relative_lookback)
        if sector_1m is None or relative_1m is None:
            return []
        if sector_1m < self.sector_min_return:
            return []

        volume = view["volume"].astype(float)
        baseline = volume.iloc[-(self.selloff_bars + self.volume_baseline) : -self.selloff_bars].mean()
        relative_volume = float(volume.iloc[-self.selloff_bars :].mean() / baseline) if baseline > 0 else 0.0
        if relative_volume >= self.max_relative_volume:
            return []

        # The ranking metric: how much more the stock fell than its sector.
        idiosyncratic_drop = -relative_1m
        last_close = float(view["close"].iloc[-1])
        return [
            RawSignal(
                strategy_name=self.name,
                strategy_version=self.version,
                symbol=str(view.attrs.get("symbol", "UNKNOWN")),
                side="long",
                score=Decimal(str(round(idiosyncratic_drop, 4))),
                suggested_entry=Decimal(str(round(last_close, 4))),
                suggested_stop=None,
                metadata={
                    "rsi_2": round(float(rsi_now), 2),
                    "sma_200": round(float(trend), 4),
                    "stock_return_1m": round(float(relative_1m + sector_1m), 4),
                    "sector_return_1m": round(float(sector_1m), 4),
                    "idiosyncratic_drop": round(idiosyncratic_drop, 4),
                    "relative_volume": round(relative_volume, 2),
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
        if len(view) < self.exit_sma_period + self.rsi_period + 1:
            return None
        price = view["adj_close"].astype(float)

        bars_held = int((view.index.date > position.opened_at.date()).sum())
        if bars_held >= self.max_holding_bars:
            return ExitDecision(reason="time_stop", qty=position.qty)

        if price.iloc[-1] > sma(price, self.exit_sma_period).iloc[-1]:
            return ExitDecision(reason="recovered_above_sma5", qty=position.qty)
        if rsi(price, self.rsi_period).iloc[-1] > self.rsi_exit:
            return ExitDecision(reason="rsi_recovered", qty=position.qty)
        return None
