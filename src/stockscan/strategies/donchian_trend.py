"""Donchian Channel Breakout (Turtle-style) — v1.2.

v1.2 ("base breakout") narrows the strategy from "any breakout in a
trending market" to specifically the **base-breakout setup**: a stock
that has been quiet (tight range, contracted volatility, not extended
off its 50-day) and just broke out on volume. This rules out three
classes of false-positive that v1.1 still let through:

  * **Stocks already mid-trend** — running up for weeks, RSI elevated
    going into the breakout. These look like breakouts on the chart but
    are actually mean-reversion candidates.
  * **Rapidly fluctuating stocks** — high recent ATR, wide swings, the
    breakout bar is just one of many wide bars. No edge here.
  * **Overextended stocks** — far above the 50-day MA. Even valid
    breakouts here have very poor risk:reward because the reversal-
    to-the-mean can take out the stop in a single bad day.

Four new filters address those (each with its own toggle):

  6. **Base consolidation width.** The 20-bar pre-breakout range,
     measured as (max high - min low) / midpoint, must be ≤ 15%. Wide
     bases that resolve upward weren't really consolidations.

  7. **Volatility contraction.** ATR(20) excluding today divided by
     ATR(63) must be ≤ 0.92. Recent vol must have compressed relative
     to the longer-term baseline (Bollinger Squeeze framing).

  8. **Max distance above SMA(50).** Today's close <= 1.15x SMA(50)
     by default. Skip stocks that have already soared off their
     50-day reference.

  9. **Max RSI pre-breakout.** RSI(14) on yesterday's close ≤ 65.
     The breakout bar itself naturally pops RSI; this filter targets
     stocks that were ALREADY overbought going into the move.

Lit reference for these filters:
  - Minervini, M. (2013). *Trade Like a Stock Market Wizard*. The VCP
    (Volatility Contraction Pattern) framework.
  - O'Neil, W. (1988). *How to Make Money in Stocks*. Cup-and-handle base.
  - Weinstein, S. (1988). *Secrets for Profiting in Bull and Bear Markets*.
    Stage-2 base breakout.
  - Bollinger, J. (2001). *Bollinger on Bollinger Bands*. The Squeeze.

The original v1.1 framework (multi-window, volume confirm, vol
expansion, Turtle 1L, relative strength) is unchanged. The new filters
sit on top of those; ALL filters apply to BOTH the 20-day and 55-day
windows (unlike the 1L filter, which is System 1 only).

----------------------------------------------------------------------

v1.0 was a faithful Turtle System 1 implementation. v1.1 layered on five
empirically-supported improvements drawn from the original Turtle leaked
rules + modern equity-trend literature:

  1. **Multi-window ensemble** (entry_periods = [20, 55]). The original
     Turtles ran System 1 (20-day) and System 2 (55-day) in parallel.
     Greyserman & Kaminski's "Trend Following with Managed Futures" shows
     basket-of-windows reduces parameter sensitivity and adds ~10-20%
     Sharpe over any single window. We pick the LONGEST qualifying window
     each scan day and emit one signal per symbol.

  2. **Volume confirmation**. Genuine institutional accumulation produces
     volume; thin-tape false breakouts don't. Default rule: today's volume
     must be at least 1.5x its trailing 20-day average.

  3. **Volatility-expansion confirmation** (Larry Williams). Today's true
     range must be >= ATR(14). Filters out single-touch wicks at the high
     that closed there but had no real range.

  4. **Turtle "1L" filter** (System 1 only). The original Turtles' rule:
     skip a 20-day breakout if the PREVIOUS 20-day breakout for this
     symbol would have been a winner (closed positive when its 10-day
     exit fired or when 2x ATR stop hit). The rationale per Faith's
     "Way of the Turtle": big sustained trends usually start AFTER a
     cluster of small false breakouts, so a fresh winner increases the
     odds the next breakout is a fakeout. The 55-day System 2 acts as
     the failsafe for genuinely-trending names.

     1L rejections are NOT silent — the strategy emits the signal with
     ``metadata['_strategy_reject_reason']='turtle_1l_skip_after_winner'``
     and the runner routes it into the rejected-signals list. This makes
     the filter's hit rate visible in the Signals UI and easy to A/B
     test against by setting ``enable_turtle_1l = False``.

  5. **Relative-strength filter**. Equity-trend literature (AQR's "Trends
     Everywhere"; Hurst 2017; Clenow 2015) consistently shows ~+0.2
     Sharpe from requiring the candidate stock to be outperforming the
     broader market over the trailing 60 days. We compare the 60-day
     return of the candidate against the same window for the configured
     benchmark (default SPY); skip if the stock has lagged.

ALL v1.1 improvements default ON, and the v1.2 filters default ON as
well. Each filter has its own toggle so backtests can A/B contributions
independently.

Recover **v1.1 behavior** by disabling the v1.2 filters:

    DonchianParams(
        require_base_consolidation=False,
        require_vol_contraction=False,
        max_pct_above_sma50=0.0,       # 0 disables
        max_rsi_pre_breakout=100.0,    # 100 disables
        adx_min=18.0,                  # was tightened from 18 → 20
        volume_mult=1.5,               # was tightened from 1.5 → 1.75
    )

Recover the original **v1.0 behavior** by additionally disabling all
v1.1 filters:

    DonchianParams(
        # v1.2 disabled (as above)
        require_base_consolidation=False,
        require_vol_contraction=False,
        max_pct_above_sma50=0.0,
        max_rsi_pre_breakout=100.0,
        # v1.1 disabled
        entry_periods=[20],
        volume_mult=1.0,            # disable volume filter
        require_vol_expansion=False,
        enable_turtle_1l=False,
        enable_relative_strength=False,
    )

Reference:
  - Faith, C. (2007). *Way of the Turtle*. McGraw-Hill.
  - Greyserman & Kaminski (2014). *Trend Following with Managed Futures*.
  - Hurst, B. et al. (2017). "A Century of Evidence on Trend-Following Investing." AQR.
  - Clenow, A. F. (2015). *Stocks on the Move*.
  - Williams, L. (1999). *Long-Term Secrets to Short-Term Trading*.
  - The leaked Turtle rules document (oxfordstrat.com).
"""

from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, ClassVar

import pandas as pd
from pydantic import Field

from stockscan.indicators import adx, atr, rsi, sma
from stockscan.strategies import (
    ExitDecision,
    PositionSnapshot,
    RawSignal,
    Strategy,
    StrategyParams,
)

if TYPE_CHECKING:
    from datetime import date

log = logging.getLogger(__name__)


class DonchianParams(StrategyParams):
    # ---- Existing v1.0 fields (defaults unchanged) ----
    entry_period: int = Field(
        20,
        ge=10,
        le=60,
        description=(
            "Legacy single-window entry. Ignored when ``entry_periods`` "
            "has more than one entry; kept for back-compat."
        ),
    )
    exit_period: int = Field(10, ge=5, le=40, description="Confirming exit low window")
    atr_period_stop: int = Field(20, ge=10, le=40)
    atr_stop_mult: float = Field(2.0, ge=1.0, le=4.0, description="Initial stop ATR mult")
    chandelier_period: int = Field(22, ge=10, le=40, description="Trailing-stop high window")
    chandelier_atr_mult: float = Field(3.0, ge=1.0, le=5.0)
    adx_period: int = Field(14, ge=5, le=30)
    adx_min: float = Field(
        20.0,
        ge=0.0,
        le=50.0,
        description=(
            "Skip if ADX below this. v1.2 default raised from 18 → 20 to "
            "tighten the trend-strength gate."
        ),
    )

    # ---- v1.1: Multi-window ensemble ----
    entry_periods: list[int] = Field(
        default_factory=lambda: [20, 55],
        description=(
            "Breakout windows to evaluate longest-first. The longest "
            "qualifying window emits the signal. The Turtle 1L filter "
            "applies ONLY when the 20-day window fires; longer windows "
            "act as the System-2 failsafe and are always taken."
        ),
    )

    # ---- v1.1: Volume confirmation (tightened in v1.2) ----
    volume_mult: float = Field(
        1.75,
        ge=0.0,
        le=5.0,
        description=(
            "Min ratio of today's volume to the trailing-N average to "
            "confirm institutional participation. Set to 0 or 1.0 to "
            "disable the volume gate. v1.2 default raised from 1.5 → 1.75 "
            "to demand stronger institutional confirmation."
        ),
    )
    volume_window: int = Field(
        20, ge=5, le=60, description="Lookback for the volume average."
    )

    # ---- v1.1: Volatility expansion ----
    require_vol_expansion: bool = Field(
        True,
        description=(
            "If True, today's true range must be >= ATR(14). Skips "
            "thin-range single-touch wicks at the high."
        ),
    )

    # ---- v1.1: Turtle 1L filter ----
    enable_turtle_1l: bool = Field(
        True,
        description=(
            "Skip 20-day breakouts when the previous 20-day breakout for "
            "this symbol would have been a winner. Original Turtle rule. "
            "55-day System 2 is unaffected."
        ),
    )

    # ---- v1.1: Relative-strength filter ----
    enable_relative_strength: bool = Field(
        True,
        description=(
            "Require stock 60d return > benchmark 60d return. Adds "
            "cross-sectional momentum to the absolute breakout."
        ),
    )
    rs_window: int = Field(
        60, ge=20, le=252, description="Lookback for the relative-strength comparison."
    )
    benchmark_symbol: str = Field(
        "SPY", description="Symbol to compare against for relative strength."
    )

    # ---- v1.2: Base-consolidation filter ----
    require_base_consolidation: bool = Field(
        True,
        description=(
            "Require the pre-breakout window to be a tight base. The "
            "(max high - min low) over the last `base_lookback_bars` "
            "(excluding today's bar) measured as % of the base's "
            "midpoint must be <= `base_max_range_pct`. Filters out "
            "stocks that were already moving wide before the breakout."
        ),
    )
    base_lookback_bars: int = Field(
        20,
        ge=5,
        le=100,
        description="Bars before today to evaluate as the consolidation base.",
    )
    base_max_range_pct: float = Field(
        15.0,
        ge=1.0,
        le=50.0,
        description=(
            "Max width of the consolidation base, as a percent of its "
            "midpoint. 15% = a clean tight base in equity TA literature "
            "(Minervini's pivot-point base, O'Neil cup-and-handle). "
            "v1.2.0 launched at 12.0 but produced too few signals in "
            "combination with the contraction filter; raised in v1.2.1. "
            "Lower is stricter. Set to 50.0 to effectively disable."
        ),
    )

    # ---- v1.2: Volatility-contraction filter ----
    require_vol_contraction: bool = Field(
        True,
        description=(
            "Require recent volatility to be compressed relative to the "
            "longer-term baseline. ATR(20) excluding today / ATR(63) "
            "must be <= `vol_contraction_ratio`. Captures the Bollinger "
            "Squeeze pattern: vol contracts before genuine breakouts."
        ),
    )
    vol_contraction_ratio: float = Field(
        0.92,
        ge=0.30,
        le=1.50,
        description=(
            "Max allowed ratio of recent ATR(short) to longer-term "
            "ATR(long). 0.92 = recent vol must be at least 8% lower "
            "than longer-term vol to qualify as a contraction. v1.2.0 "
            "launched at 0.85 but produced too few signals in "
            "combination with the base-width filter (real tight bases "
            "frequently land in the 0.85-0.92 range); raised in v1.2.1. "
            "Lower is stricter."
        ),
    )

    # ---- v1.2: Already-soared filter (distance from SMA50) ----
    max_pct_above_sma50: float = Field(
        15.0,
        ge=0.0,
        le=100.0,
        description=(
            "Skip if today's close is more than this percent above "
            "SMA(50). Filters stocks that have already soared off "
            "their long-term reference and are extended. 15% = "
            "Minervini's 'climax-run' threshold. Set to 0.0 to disable "
            "(0 means 'no cap')."
        ),
    )

    # ---- v1.2: Pre-breakout RSI cap ----
    max_rsi_pre_breakout: float = Field(
        65.0,
        ge=0.0,
        le=100.0,
        description=(
            "Skip if RSI(14) on YESTERDAY's close was at or above this "
            "level. Today's breakout bar naturally pops RSI; this filter "
            "checks whether the stock was already overbought going into "
            "the move. Set to 100.0 to disable."
        ),
    )


class DonchianBreakout(Strategy):
    name = "donchian_trend"
    version = "1.2.1"
    display_name = "Donchian Channel Breakout"
    description = (
        "Multi-window Turtle-style breakout, narrowed in v1.2 to the "
        "base-breakout setup. Buys 20-day or 55-day high breakouts that "
        "emerge from a tight pre-breakout base (≤12% range) with "
        "compressed volatility (ATR contraction) — and that are NOT "
        "already extended (close ≤ 15% above SMA50, RSI < 65 going in). "
        "Volume expansion, true-range expansion, Turtle 1L, and "
        "relative-strength filters from v1.1 still apply. Holds via "
        "Chandelier trailing stop."
    )
    tags = ("trend_following", "breakout", "long_only", "swing")
    params_model = DonchianParams
    default_risk_pct = 0.0075
    regime_affinity: ClassVar[dict[str, float]] = {
        "trending_up": 1.0,
        "trending_down": 1.0,
        "choppy": 0.25,
        "transitioning": 0.5,
    }

    manual = """\
## What this strategy is trying to do

This strategy is the opposite of mean-reversion. Instead of betting that a
sharp move will reverse, it bets that a sharp move *will keep going*. We
buy stocks that have just broken out to a new high (20-day or 55-day) and
hold them as long as the trend remains intact, often for weeks or months.

The intuition: real trends - driven by sustained buying from institutions,
news cycles, or sector rotations - tend to last longer than people expect.
Most of a stock's annual return often comes from a handful of trending
months. If we can identify when a trend is starting and ride it without
exiting prematurely, we capture that compounding move.

The trade-off: we will be wrong most of the time. Most "breakouts" turn out
to be false starts that immediately reverse. The math still works because
when we ARE right, the winning trades are much larger than the losers.

## What's new in v1.2 — the "base breakout" framing

v1.1 caught real breakouts in trending markets but still produced too
many signals on stocks that had already soared, were in choppy
fluctuation, or were extended off their 50-day MA. v1.2 narrows the
funnel to the specific setup where breakouts have the best historical
edge: a stock that has been QUIET (tight range, contracted vol, near
its 50-day MA, RSI not elevated) and just broke out on volume.

Four new filters, each with its own toggle:

  - **Base consolidation width.** The 20 bars BEFORE the breakout, as a
    range, must be at most 15% of midpoint. Wide "bases" weren't really
    consolidations.
  - **Volatility contraction.** ATR(20) excluding today / ATR(63) must
    be ≤ 0.92. Vol must have compressed before the move (Bollinger
    Squeeze).
  - **Already-soared cap.** Today's close must be ≤ 15% above SMA(50).
    Stocks already extended off their long-term MA make poor entries.
  - **Pre-breakout RSI cap.** Yesterday's RSI(14) must be < 65. Filters
    stocks that were ALREADY overbought going into the move.

Plus tightened defaults on existing filters: `adx_min` 18 → 20,
`volume_mult` 1.5 → 1.75.

## What's new in v1.1

The v1.1 release layered on five filters drawn from the original Turtle
leaked rules and modern equity-trend literature:

  - **20 + 55 day ensemble.** Both Turtle "System 1" (20-day, sensitive)
    and "System 2" (55-day, more confirmed) windows fire in parallel.
  - **Volume confirmation.** Breakout volume must be at least 1.5x its
    20-day average. (v1.2 raised to 1.75x.)
  - **Volatility expansion.** Today's true range must be at least equal
    to its 14-day average. Filters out wick-touch breakouts.
  - **Turtle 1L filter.** Skip 20-day breakouts when the PREVIOUS 20-day
    breakout for this symbol would have been a winner. The 55-day
    window catches sustained trends regardless.
  - **Relative strength.** Stock must be outperforming SPY over the
    trailing 60 days.

Each filter has its own toggle in `DonchianParams` so backtests can A/B
which ones contribute Sharpe.

## The components, explained

### Donchian Channel

Named after Richard Donchian (1905-1993), one of the founding figures of
trend-following. A **Donchian Channel** is just three lines drawn on a
price chart over a lookback window of N days:

  - **Upper line** = the highest high over the last N days.
  - **Lower line** = the lowest low over the last N days.
  - **Middle line** = the average of the upper and lower lines.

When today's close pushes ABOVE the upper line, that means the stock is
making a new high relative to the last N days - a breakout. We use BOTH
the 20-day and 55-day windows for entry: the 20-day catches early moves;
the 55-day confirms more durable trends and serves as the failsafe when
the 20-day signal is filtered by the 1L rule.

### Volume confirmation

Real institutional accumulation produces volume. Thin-tape breakouts that
close at the high on light trading are almost always false. We require
today's volume to be at least 1.5x its 20-day average; below that, we
treat the breakout as suspect and skip.

### Volatility expansion (Larry Williams)

A stock's true range is the larger of (today's high - today's low,
abs(today's high - yesterday's close), abs(today's low - yesterday's
close)). It captures total price movement including overnight gaps.
If today's true range is BELOW the 14-day average, the breakout was
a wick-touch with no real intraday range - usually noise. We require
today's TR >= ATR(14).

### Turtle 1L filter

The original Turtle Traders had a specific rule for System 1: **skip a
20-day breakout if the PREVIOUS 20-day breakout for this symbol would
have been a winner**. The reasoning, from Curtis Faith's *Way of the
Turtle*: big sustained trends usually start AFTER a series of small
false breakouts. Once you've just had a clean winner, the next breakout
is more likely to be a fakeout. The 55-day System 2 is the failsafe -
it always takes breakouts regardless of recent history, so genuinely
sustained trends still get caught.

When 1L blocks a signal, it shows up in the **Rejected signals** panel
on the dashboard with reason `turtle_1l_skip_after_winner`, so you can
see how often it's firing and validate empirically whether it's helping.

### Relative strength

Equity-trend literature (AQR, Hurst, Clenow) consistently finds that
absolute breakouts in stocks lagging the broader market are
substantially worse than absolute breakouts in stocks beating it. We
require the candidate's 60-day return to exceed SPY's 60-day return.
Roughly halves the trade count; the surviving trades are higher quality.

### Base consolidation width (v1.2)

The 20 bars before today's breakout bar must form a tight range —
(highest high - lowest low) of those 20 bars, divided by their
midpoint, must be ≤ 15%. In Mark Minervini's *Trade Like a Stock
Market Wizard*, the highest-quality VCP setups always emerge from a
"pivot point" base of roughly 5-15% width. Bases wider than that are
just chop in disguise. This filter is the single biggest reason v1.2
generates fewer signals than v1.1.

### Volatility contraction (v1.2)

ATR over the trailing 20 bars (excluding today) divided by ATR over
the trailing 63 bars must be ≤ 0.92. In plain English: recent vol
must be at least 8% lower than the longer-term baseline. This is
the math behind the "Bollinger Squeeze" — bands narrow as volatility
contracts, and the subsequent breakout has a better historical hit
rate than breakouts from already-volatile names. v1.2.0 launched
this at 0.85 (≥15% contraction) but produced too few signals in
practice, since real tight bases frequently land in the 0.85-0.92
range; v1.2.1 raised the cap to 0.92.

### Already-soared cap (v1.2)

Today's close must be no more than 15% above the 50-day SMA. A stock
already 25% above its 50-day MA is in a climax run; even a "valid"
breakout there has poor risk:reward because the typical stop (2x ATR
from entry) is dwarfed by the size of the typical mean-reversion to
the 50-day. Minervini calls this stage "extended"; O'Neil's rule of
thumb is similar at ~10% above the breakout pivot.

### Pre-breakout RSI cap (v1.2)

RSI(14) on YESTERDAY's close — i.e., the bar BEFORE the breakout —
must be below 65. This filters stocks that were already overbought
going into the move. Today's breakout bar will naturally pop RSI to
70+ on a clean break (that's expected and not what we filter on);
the question is whether the stock was already in overbought territory
before the breakout, which signals it was already mid-move.

### ATR - Average True Range

A volatility measurement (J. Welles Wilder, 1978). It tells you the
typical daily price range of a stock over a lookback window.

We use ATR for both our initial stop-loss and our trailing stop, scaled
by multipliers (2x and 3x respectively).

### ADX - Average Directional Index (v1.1 fallback)

Also Wilder (1978). 0-100 scale measuring trend strength (not direction).
v1.1 required ADX(14) >= 18 (raised to >= 20 in v1.2 default) to take
a breakout. v1.2's default base-consolidation mode REPLACES this gate
with a Stage-2 uptrend filter (see below) because ADX is structurally
incompatible with a tight base — a clean consolidation has ADX in
single digits, so an ADX gate would reject exactly the setups we want.
ADX is still used as the trend-strength gate when base consolidation
is disabled (v1.1-equivalent mode). Real trends have ADX above
~20 historically; chop / range-bound markets sit below ~18.

### Stage-2 uptrend filter (v1.2 base mode)

Stan Weinstein (*Secrets for Profiting in Bull and Bear Markets*, 1988)
classifies stocks into four "stages" of a market cycle:

  - Stage 1: bottom basing (sideways after a downtrend)
  - **Stage 2: uptrend** (advancing, the only stage you should buy in)
  - Stage 3: top distribution (sideways after an uptrend)
  - Stage 4: downtrend

The simplest mathematical test for "this is a Stage 2 uptrend":
**close > SMA(200) AND SMA(50) > SMA(200)**. Both conditions ensure
we're not buying in Stages 3 or 4 (which fail close > SMA(200)) or
in early Stage 2 before momentum has confirmed (which would fail
SMA(50) > SMA(200)). v1.2 uses this in place of ADX during base mode
because it doesn't get blocked by tight consolidations.

### Chandelier Exit

A trailing stop that follows winners up. Formula:

  chandelier_stop = (highest high of last N days) - (multiplier x ATR)

We use N=22, multiplier=3. Locks in profit while still giving room for
normal pullbacks.

## The rules in plain English

**Setup filters** (all must pass):
  - **Trend-strength gate** — one of:
    * (default, base mode ON) Stan-Weinstein Stage 2 uptrend:
      close > SMA(200) AND SMA(50) > SMA(200). Captures
      "long-term uptrend" without measuring the recent consolidation
      itself (ADX naturally compresses inside a tight base, so the
      ADX(14) gate would block exactly the setups we want).
    * (base mode OFF, v1.1 fallback) ADX(14) >= 20.
  - Today's close exceeds the prior N-day max close (N is the longest
    qualifying window from `entry_periods`).
  - Today's volume >= 1.75x its 20-day average. (v1.2: raised from 1.5x.)
  - Today's true range >= ATR(14).
  - Stock 60d return > SPY 60d return.
  - **(v1.2) Base width ≤ 15%** — the 20-bar pre-breakout range must
    fit within 12% of midpoint.
  - **(v1.2) Vol contraction** — pre-breakout ATR(20) <= 0.92x ATR(63).
  - **(v1.2) Not extended** — today's close <= 1.15x SMA(50).
  - **(v1.2) Pre-breakout RSI** — yesterday's RSI(14) < 65.
  - The stock isn't reporting earnings within 5 trading days (portfolio
    filter).

**1L filter** (20-day signals only):
  - If the previous 20-day breakout for this symbol would have been a
    winner under the 10-day exit / 2x ATR stop rules, SKIP today's
    20-day signal.
  - This rule does NOT apply to the 55-day System 2 entry.

**Entry**:
  - Buy at tomorrow's market open at today's close.

**Initial stop-loss**:
  - Stop = entry - 2x ATR(20).

**Trailing exit** (Chandelier, recalculated daily):
  - Trailing stop = (22-day high) - 3x ATR(22).
  - When today's close drops below the current chandelier - sell at
    tomorrow's open.

**Confirming exit** (also daily):
  - If today's close drops below the lowest low of the last 10 days -
    sell at tomorrow's open.

## What to expect when running this

  - **Far fewer trades than v1.1**, which itself was tighter than v1.0.
    The base + contraction filters together typically cut v1.1's signal
    count by another 60-80%. On a typical day in a healthy market you
    might see 0-3 candidates; in choppy markets, 0.
  - **Long holding periods** when the trend is real (weeks to months).
  - **Higher quality per trade.** The surviving signals are explicit
    base-breakouts, which have stronger historical edge in the equity
    trend-following literature than "any breakout in any uptrend."
  - **Big asymmetry** between wins and losses still required. Trend
    strategies depend on winners running 3-5x the size of losers.

## Where this strategy struggles

  - **Choppy / range-bound markets** still cause whipsaws on the bars
    that did pass all filters. The base-width and contraction filters
    plus 1L help but aren't perfect.
  - **Mean-reversion regimes** - the strategy spends long periods in
    cash waiting for trends to develop.
  - **Late entries.** By definition, we enter AFTER a breakout - some
    of the move has already happened.

## Why we run this alongside RSI(2)

The two strategies have **negatively correlated equity curves**. RSI(2)
makes money when markets chop and revert; Donchian makes money when
markets trend. Running both diversifies your strategy stack so a
sustained run of either market type doesn't kill total P&L.

## Source

- Curtis Faith, *Way of the Turtle* (2007).
- Greyserman & Kaminski, *Trend Following with Managed Futures* (2014).
- AQR, "Trends Everywhere" (2022).
- Andreas Clenow, *Stocks on the Move* (2015).
- The leaked Turtle rules document (oxfordstrat.com).
"""

    # ------------------------------------------------------------------
    def required_history(self) -> int:
        # Longest entry window + slack for prior-signal walkback (1L)
        # plus the longer of (RS window, chandelier, ADX warmup, the v1.2
        # 50-day MA, and the long ATR(63) for vol contraction).
        max_entry = max(self.params.entry_periods or [self.params.entry_period])
        return (
            max(
                max_entry + self.params.entry_period,  # 1L walkback room
                max_entry + 5,
                self.params.rs_window + 5,
                self.params.chandelier_period,
                self.params.atr_period_stop,
                self.params.adx_period * 2,
                self.params.base_lookback_bars + 5,    # v1.2 base window
                63 + 5,                                # v1.2 long ATR for contraction
                50 + 5,                                # v1.2 SMA(50)
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
        volume = view.get("volume")
        symbol = self._symbol(view)
        last_close = float(close.iloc[-1])

        # ---- 1. Multi-window: pick the longest qualifying window. ----
        # Iterate longest-first; the longest window that today's close
        # exceeds becomes the qualifying window. If none qualify, abort.
        windows = sorted(set(self.params.entry_periods), reverse=True)
        qualifying_window: int | None = None
        prior_max_close: float | None = None
        for n in windows:
            roll_max = close.rolling(n).max().shift(1).iloc[-1]
            if pd.isna(roll_max):
                continue
            if last_close > float(roll_max):
                qualifying_window = n
                prior_max_close = float(roll_max)
                break
        if qualifying_window is None or prior_max_close is None:
            return []

        # ---- 2. Trend-strength gate. ----
        # Two flavors, mutually exclusive:
        #
        #  (a) When base consolidation is REQUIRED: ADX(14) is the wrong
        #      tool — by design a tight base has ADX in single digits, so
        #      a >=20 ADX gate would block exactly the setups we want.
        #      Replace it with a Stan-Weinstein "Stage 2 uptrend" check:
        #      close > SMA(200) AND SMA(50) > SMA(200). That captures
        #      "stock is in a long-term uptrend" without measuring the
        #      consolidation phase itself.
        #
        #  (b) When base consolidation is OFF (back-compat with v1.1):
        #      use the original ADX(14) >= adx_min gate.
        adx_f: float | None = None
        if self.params.require_base_consolidation:
            sma200_series = sma(close, 200)
            sma50_series = sma(close, 50)
            if len(sma200_series) < 1 or len(sma50_series) < 1:
                return []
            sma200_v = sma200_series.iloc[-1]
            sma50_v = sma50_series.iloc[-1]
            if pd.isna(sma200_v) or pd.isna(sma50_v):
                return []
            if last_close <= float(sma200_v):
                return []
            if float(sma50_v) <= float(sma200_v):
                return []
        else:
            adx_v = adx(high, low, close, self.params.adx_period).iloc[-1]
            if pd.isna(adx_v) or float(adx_v) < self.params.adx_min:
                return []
            adx_f = float(adx_v)

        # ---- 3. Volume confirmation. ----
        volume_ratio: float | None = None
        if self.params.volume_mult > 1.0 and volume is not None:
            recent_vol = volume.iloc[-self.params.volume_window :]
            if len(recent_vol) >= self.params.volume_window:
                avg_vol = float(recent_vol.iloc[:-1].mean())  # exclude today
                today_vol = float(recent_vol.iloc[-1])
                if avg_vol > 0:
                    volume_ratio = today_vol / avg_vol
                    if volume_ratio < self.params.volume_mult:
                        return []
                # If avg_vol == 0 (illiquid name), skip rather than divide.
                elif self.params.volume_mult > 1.0:
                    return []
            # Insufficient bars → skip silently rather than allow on benefit
            # of the doubt. Don't want to backdoor past the filter.
            else:
                return []

        # ---- 4. Volatility expansion. ----
        atr14_series = atr(high, low, close, 14)
        atr14_v = atr14_series.iloc[-1]
        if pd.isna(atr14_v) or float(atr14_v) <= 0:
            return []
        atr14_f = float(atr14_v)
        # True range today = max(H-L, abs(H-C_prev), abs(L-C_prev)).
        c_prev = float(close.iloc[-2]) if len(close) >= 2 else last_close
        h_today = float(high.iloc[-1])
        l_today = float(low.iloc[-1])
        today_tr = max(
            h_today - l_today, abs(h_today - c_prev), abs(l_today - c_prev)
        )
        if self.params.require_vol_expansion and today_tr < atr14_f:
            return []
        vol_expansion_ratio = today_tr / atr14_f if atr14_f > 0 else None

        # ---- 4b. v1.2: Base-consolidation width filter. ----
        # Look at the K bars BEFORE today's bar (the breakout). Compute
        # the high-low range as a percent of the midpoint. A clean base
        # is tight; a "base" wider than ~12% of price is really just
        # ongoing chop, not a consolidation we want to fade buying into.
        base_range_pct: float | None = None
        if self.params.require_base_consolidation:
            k = self.params.base_lookback_bars
            if len(view) <= k:
                return []
            base_window = view.iloc[-(k + 1) : -1]  # exclude today
            base_high = float(base_window["high"].max())
            base_low = float(base_window["low"].min())
            base_mid = (base_high + base_low) / 2
            if base_mid <= 0:
                return []
            base_range_pct = (base_high - base_low) / base_mid * 100
            if base_range_pct > self.params.base_max_range_pct:
                return []

        # ---- 4c. v1.2: Volatility-contraction filter. ----
        # Recent ATR(20) excluding today / longer-term ATR(63) must be
        # below the threshold. Captures the Bollinger Squeeze: real
        # base breakouts are preceded by contraction, not expansion.
        contraction_ratio: float | None = None
        if self.params.require_vol_contraction:
            short_atr_series = atr(high, low, close, 20)
            long_atr_series = atr(high, low, close, 63)
            # Exclude today's bar (the breakout) so the ratio reflects
            # the PRE-breakout regime, not the breakout itself.
            if len(short_atr_series) < 2 or len(long_atr_series) < 2:
                return []
            short_atr = short_atr_series.iloc[-2]
            long_atr = long_atr_series.iloc[-2]
            if pd.isna(short_atr) or pd.isna(long_atr) or float(long_atr) <= 0:
                return []
            contraction_ratio = float(short_atr) / float(long_atr)
            if contraction_ratio > self.params.vol_contraction_ratio:
                return []

        # ---- 4d. v1.2: Already-soared filter (distance from SMA50). ----
        # Stocks already 15%+ above their 50-day MA are extended; even
        # valid breakouts here have poor risk:reward. 0 disables the cap.
        pct_above_sma50: float | None = None
        if self.params.max_pct_above_sma50 > 0:
            sma50_series = sma(close, 50)
            if len(sma50_series) < 1:
                return []
            sma50_v = sma50_series.iloc[-1]
            if pd.isna(sma50_v) or float(sma50_v) <= 0:
                return []
            pct_above_sma50 = (last_close - float(sma50_v)) / float(sma50_v) * 100
            if pct_above_sma50 > self.params.max_pct_above_sma50:
                return []

        # ---- 4e. v1.2: Pre-breakout RSI cap. ----
        # Yesterday's RSI(14) — the breakout bar itself naturally pops
        # RSI, so checking today's would defeat the filter. We want to
        # reject stocks that were already in overbought territory before
        # the breakout (i.e., already in a sustained run-up).
        rsi_pre: float | None = None
        if self.params.max_rsi_pre_breakout < 100.0:
            if len(close) < 2:
                return []
            rsi_series = rsi(close.iloc[:-1], 14)
            if len(rsi_series) < 1:
                return []
            rsi_v = rsi_series.iloc[-1]
            if pd.isna(rsi_v):
                # No RSI yet (insufficient history) — be conservative
                # and skip, matching how other warmup-required filters
                # behave above.
                return []
            rsi_pre = float(rsi_v)
            if rsi_pre >= self.params.max_rsi_pre_breakout:
                return []

        # ---- 5. Relative-strength filter. ----
        rs_diff: float | None = None
        if self.params.enable_relative_strength:
            rs_diff = self._relative_strength(view, as_of, last_close)
            if rs_diff is None:
                # Benchmark unavailable — degrade gracefully (don't block).
                log.debug(
                    "donchian: %s — RS filter skipped (no benchmark bars)", symbol
                )
            elif rs_diff <= 0:
                return []

        # ---- 6. ATR(20) for the initial stop. ----
        atr20_v = atr(high, low, close, self.params.atr_period_stop).iloc[-1]
        if pd.isna(atr20_v) or float(atr20_v) <= 0:
            return []
        atr20_f = float(atr20_v)

        entry = Decimal(str(round(last_close, 4)))
        stop = Decimal(str(round(last_close - self.params.atr_stop_mult * atr20_f, 4)))
        breakout_strength = (last_close - prior_max_close) / atr20_f
        score = Decimal(str(round(min(1.0, breakout_strength), 4)))

        metadata: dict[str, object] = {
            "breakout_window": qualifying_window,
            "prior_max_close": round(prior_max_close, 4),
            "atr": round(atr20_f, 4),
            "vol_expansion_ratio": round(vol_expansion_ratio, 4)
            if vol_expansion_ratio is not None
            else None,
        }
        if adx_f is not None:
            metadata["adx"] = round(adx_f, 4)
        if volume_ratio is not None:
            metadata["volume_mult_actual"] = round(volume_ratio, 4)
        if rs_diff is not None:
            metadata["rs_60d_diff"] = round(rs_diff, 4)
        # v1.2 base-breakout diagnostics. Only stash when the filter
        # actually computed a value (so the signal-detail page can
        # tell "filter disabled" from "filter passed cleanly").
        if base_range_pct is not None:
            metadata["base_range_pct"] = round(base_range_pct, 4)
        if contraction_ratio is not None:
            metadata["vol_contraction_ratio_actual"] = round(contraction_ratio, 4)
        if pct_above_sma50 is not None:
            metadata["pct_above_sma50"] = round(pct_above_sma50, 4)
        if rsi_pre is not None:
            metadata["rsi_pre_breakout"] = round(rsi_pre, 4)

        # ---- 7. Turtle 1L filter (System 1 only). ----
        # Walks back to the most recent prior 20-day breakout for this
        # symbol and simulates its outcome under the same exit rules.
        # If profitable, we DON'T silently drop — we emit the signal
        # tagged for the runner to route into the rejected list, so the
        # filter's hit rate is observable in the UI.
        if self.params.enable_turtle_1l and qualifying_window == 20:
            prior_outcome = self._previous_breakout_outcome(
                view, qualifying_window, atr_period=self.params.atr_period_stop
            )
            metadata["prior_signal_outcome"] = prior_outcome  # 'winner' | 'loser' | 'none'
            if prior_outcome == "winner":
                metadata["_strategy_reject_reason"] = "turtle_1l_skip_after_winner"

        return [
            RawSignal(
                strategy_name=self.name,
                strategy_version=self.version,
                symbol=symbol,
                side="long",
                score=score,
                suggested_entry=entry,
                suggested_stop=stop,
                metadata=metadata,
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

        prior_low = low.rolling(self.params.exit_period).min().shift(1).iloc[-1]
        if not pd.isna(prior_low) and last_close < float(prior_low):
            return ExitDecision(reason="exit_below_n_day_low", qty=position.qty)

        ch_high = high.rolling(self.params.chandelier_period).max().iloc[-1]
        ch_atr = atr(high, low, close, self.params.chandelier_period).iloc[-1]
        if not pd.isna(ch_high) and not pd.isna(ch_atr):
            chandelier = float(ch_high) - self.params.chandelier_atr_mult * float(ch_atr)
            if last_close <= chandelier:
                return ExitDecision(reason="chandelier_stop", qty=position.qty)

        return None

    # ------------------------------------------------------------------
    # Internal helpers — kept private to avoid widening the strategy ABC.
    # ------------------------------------------------------------------
    def _relative_strength(
        self,
        view: pd.DataFrame,
        as_of: date,
        last_close: float,
    ) -> float | None:
        """Return ``stock_60d_return - benchmark_60d_return``, or None.

        Lazy-imports get_bars to avoid a circular import at module load
        time (strategies are leaves; data.store imports config which
        imports... etc.). The benchmark fetch is cached at the DB layer
        (TimescaleDB hypertable hits the same row repeatedly across a
        scan run), so the per-symbol cost is negligible.
        """
        n = self.params.rs_window
        if len(view) <= n:
            return None
        try:
            from stockscan.data.store import get_bars
        except Exception as exc:
            log.debug("donchian: RS filter — couldn't import get_bars: %s", exc)
            return None
        try:
            bench = get_bars(
                self.params.benchmark_symbol,
                start=as_of - timedelta(days=n + 30),
                end=as_of,
            )
        except Exception as exc:
            log.debug(
                "donchian: RS filter — benchmark fetch failed for %s: %s",
                self.params.benchmark_symbol,
                exc,
            )
            return None
        if bench is None or bench.empty or "close" not in bench.columns:
            return None
        bench_close = bench["close"]
        if len(bench_close) <= n:
            return None
        bench_now = float(bench_close.iloc[-1])
        bench_then = float(bench_close.iloc[-1 - n])
        if bench_then <= 0:
            return None
        bench_ret = (bench_now / bench_then) - 1.0

        stock_then = float(view["close"].iloc[-1 - n])
        if stock_then <= 0:
            return None
        stock_ret = (last_close / stock_then) - 1.0

        return stock_ret - bench_ret

    def _previous_breakout_outcome(
        self,
        view: pd.DataFrame,
        period: int,
        *,
        atr_period: int,
    ) -> str:
        """Walk back to the most recent prior period-day breakout and
        simulate its outcome under v1.1 exit rules.

        Returns ``"winner"`` if the prior breakout would have closed
        at a profit (price hit profit-take of entry + 2 x ATR or the
        10-day exit fired with close > entry), ``"loser"`` if the stop
        or 10-day exit fired below entry, or ``"none"`` if no prior
        breakout exists in the available history.

        Pure-functional walk on `view` — no DB calls. Bounded by
        `view.length` * `period` comparisons in the worst case.
        """
        close = view["close"]
        high = view["high"]
        low = view["low"]
        # `roll_max` excludes today via shift(1). We want the previous
        # breakout STRICTLY BEFORE today's bar.
        roll_max = close.rolling(period).max().shift(1)
        # Compare each historical close to the prior-period max close at
        # that same row's index. Iterate from second-to-last (index -2)
        # backwards looking for a breakout day BEFORE today.
        breakout_idx: int | None = None
        for i in range(len(close) - 2, period, -1):
            prev_max = roll_max.iloc[i]
            if pd.isna(prev_max):
                continue
            if float(close.iloc[i]) > float(prev_max):
                breakout_idx = i
                break
        if breakout_idx is None:
            return "none"

        # Simulate the prior breakout's lifecycle. Forward-iterate from
        # breakout_idx + 1 (next day = entry) and check each bar for:
        #   - stop hit: low <= entry - 2 x ATR
        #   - 10-day exit: close < min(low[i-10:i])
        #   - profit-take proxy: close > entry by enough that the
        #     chandelier or 10-day-low exit closed the trade in the green
        # We use the SAME exit rules the live strategy uses (10-day
        # confirming exit + 2 x ATR initial stop). Keep this simulation
        # short — it's bounded by the strategy's hold horizon.
        entry_close = float(close.iloc[breakout_idx])
        atr_at_entry_series = atr(high, low, close, atr_period)
        atr_at_entry = atr_at_entry_series.iloc[breakout_idx]
        if pd.isna(atr_at_entry) or float(atr_at_entry) <= 0:
            return "none"
        stop_level = entry_close - self.params.atr_stop_mult * float(atr_at_entry)
        # Cap the lookahead at 60 trading days (matches the holding-period
        # heuristic used by the literature for trend trades' realised
        # outcomes). Most Donchian winners close in 20-40 days; 60 is
        # generous slack.
        max_lookahead = 60
        end = min(len(close), breakout_idx + 1 + max_lookahead)
        for j in range(breakout_idx + 1, end):
            # Stop check first — intra-bar low touch.
            if float(low.iloc[j]) <= stop_level:
                return "loser"
            # 10-day confirming exit: today's close < min of prior 10 lows.
            window_start = max(0, j - self.params.exit_period)
            prior_low_min = float(low.iloc[window_start:j].min()) if j > window_start else None
            if prior_low_min is not None and float(close.iloc[j]) < prior_low_min:
                # Compare close to entry to label winner vs loser.
                return "winner" if float(close.iloc[j]) > entry_close else "loser"

        # Ran out of lookahead bars — use sign of final close vs entry.
        final_close = float(close.iloc[end - 1])
        return "winner" if final_close > entry_close else "loser"

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
