"""Reversal Swing — trade tops & bottoms off a strategy-owned reversal score.

The strategy reads top-to-bottom: a trader can open this file and see exactly
how it scores a setup, when it enters, when it exits, and how each piece is
weighted. Indicator primitives are imported from ``stockscan.indicators`` and
composed *here* — there is no shared "composite" function that hides the math.

  Entry (long):  reversal score ≥ entry_threshold  → a confirmed bottom.
  Exit (first to fire):
      • reversal score ≤ −exit_threshold            → a confirmed top
      • close ≤ entry − atr_stop_mult × ATR(14)     → hard stop
      • held ≥ max_holding_days                      → time stop

Long-only (E*TRADE equities): a confirmed top is an *exit*, not a short. The
loop is buy the bottom → ride to the top → exit → re-enter the next bottom.

The reversal score combines five reads, weighted locally:

  - the turn   : RSI(2) at an extreme AND hooking back        (reversal_trigger, 0.35)
  - the level  : at a confirmed swing support below price     (pivot_proximity,  0.30)
  - the leader : outperforming its sector composite           (sector_rs,        0.20)
  - the trend  : reinforce-only — historically intended to
                 boost with-trend reversals; **default weight
                 is now 0 (see v1.3.0 notes below)**           (trend_location,   0.00)
  - volume     : a Wyckoff climax/absorption bar in the last
                 5 bars scales |conviction| in [vol_floor, 1.0];
                 never flips the sign                         (volume_confirm,   ×)

Three hard gates sit in front of the math; if any one fires →
``reversal_score()`` returns ``None``:

  v1.2.0 — *turn gate*: ``reversal_trigger.raw`` must be present and > 0. The
    turn is the timing piece; without it there is no bottom, only "level +
    confirmation" which is dip-buying. (Side effect: the ``reversal_top``
    exit branch is unreachable; exits are the ATR stop and the time stop.)

  v1.4.0 — *level gate*: ``pivot_proximity.raw`` must be present and >
    ``pivot_floor`` (default 0). Firing without a confirmed support is
    firing outside the strategy's stated thesis ("buy reversals AT a level")
    and was 100% loss-correlated on TSLA across bt20–23.

  v1.3.0 — *leader-floor gate*: when ``sector_rs`` contributed, its score must
    be ≥ ``sector_rs_floor`` (default −0.5). A name in strongly negative
    relative strength isn't a "leader" — fading the dip there is the
    falling-knife trade the strategy is meant to avoid. An *abstaining*
    ``sector_rs`` (None) is NOT gated; missing data isn't penalised.

v1.3.0 also zeroed the ``trend_location`` weight (``trend_weight = 0``). The
"dip-in-uptrend is higher quality" thesis was empirically contradicted in
bt20/21/22 — with-trend setups underperformed counter-trend ones on TSLA at
Δ −0.85, −0.58 winners-vs-losers. The trend value still appears in the
breakdown for diagnostics; raise the knob to re-enable the reinforce step.

Math is in :meth:`ReversalSwing.reversal_score`, which signals() and
exit_rules() both call (and which the watchlist + CLI debug command also call
when they want to ask "what does this strategy think of this symbol?").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, ClassVar

import pandas as pd

from stockscan.indicators import (
    atr,
    pivot_proximity,
    reversal_trigger,
    sector_relative_strength,
    trend_location,
    volume_confirm,
)
from stockscan.strategies import (
    ExitDecision,
    PositionSnapshot,
    RawSignal,
    Strategy,
)


# ---------------------------------------------------------------------------
# Tiny math helpers — the strategy's two-stage composite is the only consumer.
# ---------------------------------------------------------------------------
def _clip(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _sign(x: float) -> int:
    return (x > 0) - (x < 0)


METHODOLOGY_VERSION = 2  # bumps when the scoring math changes (spec §6)


@dataclass(frozen=True, slots=True)
class ScoredResult:
    """The output of :meth:`ReversalSwing.reversal_score` — signed score plus
    per-input attribution that the signal-detail view renders."""

    score: float                                       # signed, in [-1, +1]
    breakdown: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_breakdown_json(self) -> dict[str, Any]:
        return {"score": self.score, "indicators": self.breakdown}


class ReversalSwing(Strategy):
    name = "reversal_swing"
    # 1.0.0 → 1.1.0: params_model retired; knobs now live as ClassVar constants
    #                on this class (edit-and-bump model, no DB shadow).
    # 1.1.0 → 1.2.0: reversal_score() hard-gates on the turn — entries with
    #                reversal_trigger ≤ 0 are rejected outright (was the
    #                dominant failure mode in run #20). Side effect: the
    #                reversal_top exit branch is unreachable; stops +
    #                time-stop carry exits.
    # 1.2.0 → 1.3.0: two architectural calls supported by bt22 data —
    #                (a) sector_rs floor gate: entries with sector_rs < -0.5
    #                are rejected. A name in strongly negative relative
    #                strength isn't a "leader," so fading the dip there is
    #                the falling-knife trade the strategy is trying to avoid.
    #                (b) trend_weight = 0: the "dip-in-uptrend is higher
    #                quality" thesis kept getting contradicted by the data
    #                across bt20/21/22 (with-trend setups underperformed at
    #                Δ -0.58 in bt22). Reinforce-only is disabled by default;
    #                trend_location still appears in the breakdown for
    #                transparency, with weight 0.
    # 1.3.0 → 1.4.0: pivot_proximity floor gate. bt23 showed piv = 0 was
    #                perfectly anti-correlated with success (2 losers had
    #                piv = 0, all 4 winners had piv > 0). The strategy is
    #                designed to "buy reversals AT a level"; firing without
    #                a confirmed support is firing outside the design.
    # 1.4.0 → 1.5.0: volume_confirm rework — single-bar v1 → Wyckoff
    #                multi-bar (5-bar scan). The climax/absorption bar
    #                canonically prints 1-3 bars BEFORE the hook that gates
    #                entry, so v1 was missing it on most real trades and
    #                the multiplier was flooring at vol_floor. bt26 showed
    #                C = 0.75 on every single S&P500 trade — the input was
    #                effectively dead. v2 scans the last lookback_bars
    #                (=5) bars for the strongest direction-aware climax
    #                candidate and classifies it as climax (wide spread),
    #                absorption (narrow spread), or mixed. The breakdown
    #                now also carries climax_offset/climax_kind/
    #                climax_direction for diagnostics.
    version = "1.5.0"
    display_name = "Reversal Swing (tops & bottoms)"
    description = (
        "Buys confirmed bottoms using a signed reversal score (turn + level + "
        "relative strength, scaled by a climax-volume multiplier). Long-only; "
        "exits via ATR stop or time stop."
    )
    tags = ("mean_reversion", "long_only", "swing")
    default_risk_pct = 0.01

    # ----- Tunable knobs -----
    # The strategy's identity. Change a value, bump ``version``, redeploy — the
    # live scanner picks the new value up directly from this file (no DB row
    # shadows these). Backtests of a prior version stay queryable via their
    # version tag.
    entry_threshold:  ClassVar[float] = 0.25  # go long when the reversal score ≥ this (bottom)
    exit_threshold:   ClassVar[float] = 0.35  # exit when the reversal score ≤ −this (confirmed top)
    atr_period:       ClassVar[int]   = 14    # ATR lookback for stop sizing
    atr_stop_mult:    ClassVar[float] = 2.5   # hard-stop ATR multiple
    max_holding_days: ClassVar[int]   = 30    # time stop (swing horizon)

    # Second hard gate (v1.3.0): sector_rs floor. When the leader read is
    # *present* and strongly negative, the name is a relative laggard — the
    # strategy's thesis is "buy resilient leaders showing reversal signals,"
    # so fading a dip in a laggard is exactly the wrong setup. Abstaining
    # sector_rs (None) is *not* gated — we don't penalize missing data.
    sector_rs_floor:  ClassVar[float] = -0.5

    # Third hard gate (v1.4.0): pivot_proximity floor. The strategy's thesis
    # is "buy reversals AT a level"; firing without a confirmed support to
    # lean on is dip-buying, not reversal trading. Unlike sector_rs (which
    # depends on external sector composite data), pivot is bars-only — a
    # None or zero pivot is structurally meaningful, not just missing data.
    # Strict > 0; raise above 0 to require stronger level proximity.
    pivot_floor:      ClassVar[float] = 0.0

    # Reinforce-only trend weight (v1.3.0: zeroed). The bt20/21/22 backtests
    # all showed with-trend bottoms underperforming counter-trend bottoms on
    # TSLA (Δ -0.85, -0.58 winners-vs-losers). Setting trend_weight = 0
    # neutralises the reinforce step while keeping trend_location's read in
    # the breakdown for diagnostics. Raise to 0.15 to restore the historical
    # behaviour, or higher to test "trend dominates" hypotheses.
    trend_weight:     ClassVar[float] = 0.0

    # Reversals live in chop and in pullbacks within uptrends; counter-trend
    # bottoms (downtrends) are the falling-knife trades — taken, but down-weighted.
    regime_affinity: ClassVar[dict[str, float]] = {
        "trending_up": 1.0,
        "choppy": 1.0,
        "trending_down": 0.5,
        "transitioning": 0.7,
    }

    manual = """\
## What this strategy is trying to do

Reversal Swing trades the **endings** of moves — bottoms going long, tops as
exits — using a composite score built from five technical reads. The thesis:
real reversals usually leave a footprint on multiple dimensions at once. A name
that is deep-oversold AND at a confirmed support level AND a relative leader in
its sector AND printed a climax-volume rejection bar is a much higher-quality
bottom than any single signal alone. The score is the strategy's way of
quantifying that "multiple confirmations" intuition.

Long-only by design (this is an E*TRADE equities book). A confirmed *top* is an
**exit**, not a short. The loop is: buy the bottom → ride to the top → exit →
re-enter the next bottom. The same composite that brings the trade in is what
takes it out, so entries and exits are symmetric.

## The five inputs, in trader's language

The reversal score is built from five technical reads. Each one answers a
different question about the setup. Three are *core directional* — they cast a
signed vote in [-1, +1] that gets weighted-averaged. One is *reinforce-only* —
it boosts conviction when it agrees with the core, abstains when it doesn't.
One is a *confirmation multiplier* — it scales conviction up or down but
carries no direction of its own.

### The turn — `reversal_trigger`

The timing piece. Uses **RSI(2)** (Larry Connors' 2-day RSI), a very twitchy
oscillator that can swing from near-100 to near-0 in a couple of bars. A bottom
fires only when *both*:

  - RSI(2) reached an extreme in the last two bars (default oversold ≤ 10,
    overbought ≥ 90), AND
  - The very next bar is *hooking back* — RSI is now higher than yesterday and
    the close is too.

The **hook discipline is load-bearing**. A stock pinned at RSI(2) = 5 but still
making lower lows isn't a reversal yet — it's a falling knife. The signal only
triggers once buyers actually show up. The signed `raw` value blends the
depth-of-extreme with the strength of the hook into a single number in
[-1, +1]: positive = bottom turn, negative = top turn, near zero = no turn.

### The level — `pivot_proximity`

A reversal is only worth trading **at a level**. We look for **confirmed swing
pivots** in the trailing 60 bars. A pivot is confirmed when it has `k` (=3)
bars of higher lows on each side — meaning the most recent `k` bars cannot
form a pivot yet (their right shoulder hasn't printed). This is the
no-look-ahead guarantee.

Distance to the nearest support below price (for a bottom) or resistance above
(for a top) is measured in **ATR units**, so "near" auto-scales: a $300 stock
and a $30 stock both count as "near" at the same ATR-multiple distance. The
proximity check looks at the *closest the last 3 bars came to a level*, not
just today's close — a confirmed reversal hooks AWAY from the extreme it just
tested (the turn lifts the close off the level), so a single-bar check on the
hook day would miss the test.

Signed reading: positive = near support below (a bottom), negative = near
resistance above (a top), near zero = "in the middle, no level in play."

### The leader — `sector_rs`

A reversal in a name that's been **outperforming its sector** is fundamentally
different from one in a name that's been lagging. We measure relative strength
as a 63-day **return spread**: stock return minus its equal-weight sector
composite return, saturated at ±15%, blended 70/30 with the slope of the RS
line (is the outperformance still improving?).

The thesis is pure relative *momentum* — we want sector **leaders** at
bottoms, not laggards expecting catch-up. A positive read means a leader and
reinforces a *bottom* (fade the dip in a resilient name). A negative read
means a laggard and reinforces a *top* (fade the rip in an underperformer).

Abstains gracefully when the symbol has no sector mapping or the composite
hasn't been built (returns nothing rather than guessing). When this input
abstains, the score is built from the other four — still valid, just with
less differentiation between leaders and laggards.

### The trend — `trend_location` (reinforce-only, currently weight 0)

Where price sits relative to its **50-day and 200-day SMAs**, plus the 50-day
slope. The natural read is signed: positive when price is above both averages
and they're rising (with-trend); negative when price is below and they're
falling (counter-trend).

This input is **reinforce-only** by design — it only ever *adds* conviction
when its sign agrees with the core reversal direction. When it would oppose
(a counter-trend bottom inside a real downtrend, or a "top" that's actually
a pullback inside an uptrend), it abstains rather than vetoing.

The original asymmetry was deliberate. A counter-trend bottom is still a
tradeable setup — just smaller. We didn't want a strong downtrend to
**block** every bottom signal (that's how mean-reversion gets starved in
real bears); we wanted it to **not boost** them. Bottoms in an uptrend
(dip-buys) would get the trend bonus; bottoms in a downtrend (catching the
knife) wouldn't.

**Empirical update (v1.3.0):** the data has refused to validate this thesis.
Across bt20, bt21, and bt22 — all on TSLA — with-trend bottoms underperformed
counter-trend bottoms by wide margins (winners-vs-losers Δ of −0.85 in bt20,
−0.58 in bt22). The "dip-in-uptrend is higher quality" assumption appears
to be wrong, at least for TSLA's noise regime. As of v1.3.0 the
`trend_weight` knob defaults to **0** — the reinforce step is disabled but
the trend's natural read still appears in the breakdown for diagnostics. To
re-test the original design, set `trend_weight = 0.15` and bump version.
The broader-universe backtest may yet vindicate the original thesis on a
diversified sample.

### Volume — `volume_confirm` (confirmation multiplier)

Did a **climax or absorption bar** print recently? In Wyckoff / VSA terms
there are two reversal-confirming volume patterns:

  - **Climax**: wide-spread bar with very high volume, close rejecting the
    extreme it was driving toward (down bar closing in the upper half of its
    range = bullish capitulation; up bar closing in the lower half = bearish
    distribution). The "panic / euphoria" bar at the end of a one-way move.
  - **Absorption**: narrow-spread bar with very high volume, same direction
    rejection. Institutional positioning soaking up supply (bullish) or
    demand (bearish) — quieter on the chart than a climax but just as
    load-bearing for the reversal.

The canonical Wyckoff "climax → automatic rally" pattern develops over
**2–5 bars**: the climax bar typically prints 1–3 bars BEFORE the hook
that triggers entry. v1 of this primitive only inspected the entry bar
itself and was therefore missing the climax on nearly every real trade
(bt26 showed the multiplier floored at `vol_floor` on every single S&P500
entry — the input was effectively dead). **v2 (v1.5.0) scans the last
`lookback_bars` (=5) bars** for the strongest direction-aware climax
candidate, classifies it as `climax` / `absorption` / `mixed`, and
returns its score as the multiplier base.

The output is a **multiplier** in [`vol_floor`, 1.0] (default floor 0.75).
A quiet, unremarkable window floors the multiplier at `vol_floor`; a true
climax pushes it toward 1.0 (absorption tops out slightly lower —
`spread_factor` 0.85 vs 1.0 — reflecting the relative subtlety).

The multiplier can **scale |conviction| up or down but never flip the sign**,
because volume itself carries no direction. If the directional composite says
"this is a bottom," volume can only make the strategy more or less convinced
of that bottom — it can't say "actually this is a top." The breakdown now
also carries `climax_offset` (e.g. −3 = "climax fired 3 bars ago"),
`climax_kind`, and `climax_direction` for the signal-detail view.

## How the inputs combine

Two stages, both visible in `reversal_score()` in `reversal_swing.py`. A
trader reading the strategy file can see every weight, every policy decision,
and the math inline.

### Stage 1 — directional composite (D)

The three **core directional** reads each cast a signed vote in [-1, +1] with
these weights:

  - `reversal_trigger`  → 0.35  (the turn)
  - `pivot_proximity`   → 0.30  (the level)
  - `sector_rs`         → 0.20  (the leader)
  - `trend_location`    → `trend_weight` (the trend tilt, reinforce-only —
                                          **default 0 as of v1.3.0**)

**Core average** (over inputs that returned a value, ignoring abstaining ones):

`D0 = Σ wi · si / Σ wi`

**Reinforce-only trend**: if `trend_location` is present AND its sign agrees
with `D0` AND `trend_weight > 0`, fold it in:

`D = (Σ wi · si + trend_weight · s_trend) / (Σ wi + trend_weight)`

Otherwise `D = D0`. With `trend_weight = 0` (the default), the reinforce step
never modifies D — `trend_location` becomes purely informational, surfacing
in the breakdown for the signal-detail view without influencing the score.

### Stage 2 — confirmation attenuation (C)

The volume multiplier `C ∈ [vol_floor, 1]` scales `|D|` without flipping the
sign:

`S = clip(D · C, -1, +1)`

A quiet bar floors `C` at `vol_floor` (default 0.75); a true climax/absorption
bar pushes `C` to 1.0. The strategy returns `S`.

### When does the score abstain?

If every **core** input abstains (insufficient history, missing sector
composite, etc.), the score is `None` and the strategy emits no signal.
Reinforce-only (`trend_location`) and confirmation (`volume_confirm`) never
produce a score on their own — they're a reinforcement and a multiplier
respectively, with no direction of their own.

### Hard gates: turn, level, leader-floor

Three gates sit in front of the composite math. If any one fires →
`reversal_score()` returns `None` and no signal is emitted, even when the
remaining inputs would clear the entry threshold on their own.

**1. The turn gate (added v1.2.0).** `reversal_score()` returns `None`
whenever `reversal_trigger.raw` is `None` or `≤ 0`. The turn IS the timing
piece of this strategy — without an actual hooking turn, there is no
reversal, only "level + leader confirmation" which is dip-buying. Backtest
run #20 had four entries fire with `trig ≤ 0` driven by level + trend
alone; all four lost (the worst, −1.48R, had `trig = 0.00`, `level = 1.00`,
`trend = 1.00`). The gate kills that whole failure class.

**Trade-off:** the `reversal_top` exit branch is unreachable from v1.2.0 on,
because it needed `reversal_score()` to return a negative score (a top turn
has `trig < 0`, which the turn gate now also rejects). Exits are carried by
the ATR hard stop and the time stop exclusively. In run #20, `reversal_top`
was only 3 of 13 exits anyway — the empirically dominant exits were already
stops. If top-side detection is wanted back, the right shape is a second
public method (e.g., `top_score(view, as_of)`) that mirrors `reversal_score`
but gates on `trig < 0` — keeping the bottom and top scoring paths separate.

**2. The level gate (added v1.4.0).** `pivot_proximity.raw` must be present
and strictly greater than `pivot_floor` (default 0). The strategy's stated
thesis is "buy reversals AT a level"; firing without a confirmed support to
lean on is structurally "buy near recent extremes," which is dip-buying not
reversal trading. Backtest run #23's per-input contribution table showed a
perfect separation: every loser had `piv = 0.00`, every winner had `piv > 0`.
The pattern held across all four prior runs — `piv = 0` win rates of
20/25/0/0% vs `piv > 0` win rates of 38/40/80/100%. Applied to bt23 this
gate produces 4W / 0L, total PnL ≈ +$10K.

Unlike sector_rs, pivot is a bars-only primitive; a `None` or 0 result is
structurally meaningful (no eligible confirmed pivot in the lookback window
at this distance threshold), not just missing data. Both `None` and `≤ floor`
fire the gate.

**3. The leader-floor gate (added v1.3.0).** When `sector_rs` contributed at
all, its score must be `≥ sector_rs_floor` (default `−0.5`). A name in
strongly negative relative strength isn't a "leader" — fading the dip there
is the falling-knife trade the strategy is designed to avoid. Backtest run
#22 had one entry (TSLA 2026-02-25) fire with `sector_rs = −0.74` and
returned `−0.98R` (stop_hit); the gate at `−0.5` would have killed it,
taking the run from `−$403` to `+$7,330`.

An *abstaining* `sector_rs` (returned `None` — missing sector composite or
unmapped symbol) is **not** gated: we don't penalize entries for missing
data, only for confirmed-negative leader reads. That makes the gate safely
no-op on universes / symbols where the composite isn't built yet.

## The rules in plain English

**Setup** (the bars are sliced ≤ `as_of` and bounded to the trailing 235-bar
window the score needs):

  - The reversal score is computed for the current bar.

**Entry** (today, after the close):

  - The reversal score ≥ `entry_threshold` (default **0.25** — a clear but
    not extreme bottom).
  - **We buy at tomorrow's market open** at today's close.
  - Initial stop = entry − `atr_stop_mult` × ATR(14) (default **2.5×**).

**Exits** (first to fire, daily after the close):

  1. **Reversal-top**: the reversal score ≤ −`exit_threshold` (default
     **0.35**). The same composite that brought us in is now flashing the
     opposite — exit at tomorrow's open. (Note the asymmetry: exit_threshold
     is stricter than entry_threshold, because once we're in we want a
     *confirmed* top, not a near-top.)
  2. **Hard stop**: close ≤ entry − `atr_stop_mult` × ATR(14). The trade has
     gone significantly against us — exit at tomorrow's open.
  3. **Time stop**: held ≥ `max_holding_days` (default **20** trading days).
     The thesis didn't play out within the swing horizon — move on.

## What to expect when running this

  - **Moderate trade frequency.** Reversals are stricter than dip-buys — the
    composite gates four-to-five different conditions. Expect fewer entries
    per name than RSI(2) mean-reversion, with higher per-trade edge when they
    fire.
  - **Variable hold lengths.** Most trades close on the reversal-top branch
    in 5-15 trading days; time-stop closures (less common) are usually
    marginal setups that didn't develop. Hard-stop exits are infrequent on
    quality bottoms but inevitable on counter-trend ones.
  - **Sizing asymmetry by setup.** The composite naturally sizes UP on
    with-trend bottoms (dip in an uptrend + climax volume → trend reinforces,
    volume confirms, score lands high) and sizes more cautiously on
    counter-trend bottoms (trend reinforce-only stays out, lowering D). The
    metadata records `setup_type` (`dip_in_uptrend` vs `counter_trend_bottom`)
    so you can see this in the trade log.
  - **Win rate between mean-reversion and trend-following.** Reversals
    splitting the difference is exactly the point — a quality bottom is
    rare, so the strategy is selective; when it does fire the edge per trade
    should be larger than a pure dip-buy.

## Where this strategy struggles

  - **Persistent one-way trends.** When bottoms keep failing because the
    trend is too strong, the reversal-top branch starts firing before the
    bottom develops. This is a fade-attempts game and bleeds.
  - **News-driven gaps.** A negative earnings surprise blows through the ATR
    stop. The signal filter for upcoming earnings (≤ 5 trading days, applied
    elsewhere in the pipeline) catches scheduled events; unscheduled news can
    still hit.
  - **Missing sector composite.** Without a sector mapping for a symbol, the
    leader read abstains and the score is built from a partial input set.
    Still works, but with less differentiation between leaders and laggards.
    Symbols on this strategy benefit from sector composites being built and
    refreshed regularly.

## Default parameters and why

  - `entry_threshold = 0.25` — a clear bottom signal but not an extreme one.
    Lower trades more often with weaker setups; 0.40+ trades rarely but with
    strong conviction. Tune up to slow down trade frequency; tune down to
    accept more marginal setups.
  - `exit_threshold = 0.35` — **asymmetric on purpose** (stricter than entry).
    Once we're in, we want a confirmed top, not a near-top. Tightening this
    further keeps positions through normal volatility at the cost of giving
    back more at real tops.
  - `atr_period = 14`, `atr_stop_mult = 2.5` — standard ATR window; ~2.5
    typical days of range is wide enough to survive normal noise and tight
    enough to bound loss when the thesis fails.
  - `max_holding_days = 20` — swing horizon. Most reversals develop in
    5-15 days; 20 is a generous backstop on a trade that didn't go anywhere.

## Source / references

The composite is the v2 reversal model (see `signal_scoring_spec.md` §6 in
the project repo for the original specification). The individual indicators
draw from canonical sources:

  - RSI(2) — Larry Connors and Cesar Alvarez, *Short Term Trading Strategies
    That Work* (2008).
  - Climax / absorption volume — Wyckoff's price-volume rejection bars
    (1930s).
  - Relative strength / sector leadership — modern cross-sectional momentum
    literature.
  - ATR-scaled stops — J. Welles Wilder, *New Concepts in Technical Trading
    Systems* (1978).

The combining math (weighted-average core + reinforce-only trend +
confirmation multiplier) is project-specific, calibrated for single-stock
dispersion rather than index-level signals. The weights are not yet
empirically validated — the §7 marginal-edge / orthogonality test in
`signal_scoring_spec.md` is the recommended way to validate or update them.
"""

    # ------------------------------------------------------------------
    def required_history(self) -> int:
        # Full reversal score wants trend_location's 200-day term (≥220 bars).
        return 230

    # ------------------------------------------------------------------
    def reversal_score(self, view: pd.DataFrame, as_of: date) -> ScoredResult | None:
        """Signed reversal score in [-1, +1] from five technical reads.

        Long-side thesis (positive score = a confirmed bottom):

          - the turn   : RSI(2) at an extreme AND hooking back
                         (``reversal_trigger`` — fires only when the oscillator
                         was deep oversold *and* the last bar hooks back up;
                         "don't catch the knife mid-air").
          - the level  : at a confirmed swing support below price
                         (``pivot_proximity`` — only swing lows confirmed by k
                         bars on each side are eligible; no look-ahead).
          - the leader : outperforming its sector composite
                         (``sector_relative_strength`` — the relative-momentum
                         tilt; resilient leader = bottom, laggard = top).
          - the trend  : reinforce-only — adds conviction when its sign agrees
                         with the core, abstains when it would oppose (never
                         vetoes a counter-trend bottom).
          - volume     : a Wyckoff climax/absorption bar in the last 5 bars
                         (heavy volume rejecting an extreme — the climax
                         canonically prints 1-3 bars BEFORE the hook gates
                         entry, so the scan is multi-bar) scales |conviction|;
                         can scale |D| but never flip its sign.

        The math is two stages:

          Stage 1  D = weighted average of the three core reads, then reinforced
                   by the trend read IF its sign agrees with D.
          Stage 2  S = clip(D × C, -1, +1), where C is the volume multiplier.

        Returns ``None`` if every core input abstains (insufficient history,
        missing sector composite, etc.).
        """
        if "close" not in view.columns:
            return None
        close = view["close"]
        high = view["high"] if "high" in view.columns else None
        low = view["low"] if "low" in view.columns else None
        vol = view["volume"] if "volume" in view.columns else None
        have_hlc = high is not None and low is not None
        have_hlcv = have_hlc and vol is not None

        # Each primitive returns a dict with a signed ``raw`` in [-1, +1], or
        # None when there isn't enough data. A None input abstains; the score
        # is built from whatever did contribute.
        trig = reversal_trigger(close)
        piv = pivot_proximity(high, low, close) if have_hlc else None
        rs = sector_relative_strength(view, as_of)
        trend_v = trend_location(close)
        vol_c = volume_confirm(high, low, close, vol) if have_hlcv else None

        # ----- Hard gate (added v1.2.0): the turn is the timing piece -----
        # Without an actual hooking turn (reversal_trigger.raw > 0), there is
        # no bottom — every other input only describes *context* (where the
        # price is, how the trend looks, whether the leader is leading). Run
        # #20 had four entries fire with trig ≤ 0 driven by level + trend
        # alone; all four lost (worst was −1.48R). The architecture's stated
        # thesis ("turn + level + leader") collapses to "level + leader" when
        # the turn is silent — which is dip-buying, not reversal trading.
        # Trade-off: the reversal_top *exit* branch is no longer reachable
        # (it needed a negative score, which now also returns None). Stops
        # and time-stop carry exits exclusively from here on; reversal_top
        # was only 3 of 13 exits in run #20 anyway.
        trig_s = _clip(float(trig["raw"])) if trig is not None else None
        if trig_s is None or trig_s <= 0:
            return None

        # ----- Third hard gate (v1.4.0): the level must be present -----
        # bt23's per-input contribution table showed pivot_proximity = 0 was
        # 100% loss-correlated on TSLA (2/2 entries with piv=0 lost; 4/4 with
        # piv > 0 won). The strategy's thesis is "buy reversals AT a level";
        # firing without a confirmed support to lean on is conceptually just
        # "buy near recent extremes," which is dip-buying. None of the bt22
        # winners had piv = 0, so the gate never costs us a winner across
        # the bt20-23 sample. The pattern goes back across all four runs
        # (piv=0 win rates of 20/25/0/0% vs piv>0 of 38/40/80/100%).
        piv_s = _clip(float(piv["raw"])) if piv is not None else None
        if piv_s is None or piv_s <= self.pivot_floor:
            return None

        # ----- Stage 1a — directional core (turn + level + leader) -----
        # Each casts a signed vote in [-1, +1] weighted by its share of conviction.
        # Weights live here, in the strategy, because they ARE the strategy's
        # opinion about how a reversal is shaped.
        core: list[tuple[str, float, float, dict[str, float]]] = []
        for name, weight, values in (
            ("reversal_trigger", 0.35, trig),
            ("pivot_proximity",  0.30, piv),
            ("sector_rs",        0.20, rs),
        ):
            if values is None:
                continue
            core.append((name, weight, _clip(float(values["raw"])), values))

        if not core:
            return None  # every core input abstained

        core_num = sum(w * s for _, w, s, _ in core)
        core_den = sum(w for _, w, _, _ in core)
        d_val = core_num / core_den

        breakdown: dict[str, dict[str, Any]] = {
            name: {**values, "score": s, "weight": w} for name, w, s, values in core
        }

        # ----- Second hard gate (added v1.3.0): sector_rs floor -----
        # When the leader read contributed AND is strongly negative, the name
        # is a relative laggard — exactly the kind of falling-knife setup the
        # design is meant to avoid. An abstaining sector_rs (None — no
        # composite data) is *not* gated; we don't penalize missing inputs.
        sec_score = (breakdown.get("sector_rs") or {}).get("score")
        if sec_score is not None and sec_score < self.sector_rs_floor:
            return None

        # ----- Stage 1b — reinforce-only trend (weight = self.trend_weight) -----
        # The 50/200-day trend tilt was originally designed to add conviction
        # when its sign agreed with the core direction (boosting with-trend
        # bottoms, abstaining on counter-trend ones). bt20/21/22 contradicted
        # this thesis on TSLA: with-trend setups consistently underperformed
        # counter-trend ones. Default weight is now 0 (no reinforce); the
        # value still appears in the breakdown for transparency. Raise the
        # knob to test alternative weightings.
        if trend_v is not None:
            t = _clip(float(trend_v["raw"]))
            breakdown["trend_location"] = {**trend_v, "score": t, "weight": self.trend_weight}
            if self.trend_weight > 0 and _sign(t) != 0 and _sign(t) == _sign(d_val):
                d_val = (core_num + self.trend_weight * t) / (core_den + self.trend_weight)

        # ----- Stage 2 — confirmation attenuation by volume -----
        # A climax/absorption bar (heavy volume, close rejecting the day's
        # extreme) scales |conviction| up toward 1.0; a quiet bar floors it at
        # the primitive's vol_floor. The multiplier scales |D| but never flips
        # the sign.
        if vol_c is not None:
            c_val = max(0.0, min(1.0, float(vol_c["multiplier"])))
            breakdown["volume_confirm"] = {**vol_c, "multiplier": c_val}
        else:
            c_val = 1.0

        score = _clip(d_val * c_val)
        breakdown["_meta"] = {
            "D": round(d_val, 6),
            "C": round(c_val, 6),
            "score": round(score, 6),
            "methodology_version": METHODOLOGY_VERSION,
        }
        return ScoredResult(score=score, breakdown=breakdown)

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

        result = self.reversal_score(view, as_of)
        if result is None:
            return []
        s = float(result.score)
        if s < self.entry_threshold:
            return []  # not a confirmed bottom

        close = view["close"]
        atr_v = atr(view["high"], view["low"], close, self.atr_period).iloc[-1]
        if pd.isna(atr_v) or float(atr_v) <= 0:
            return []
        last_close = float(close.iloc[-1])
        entry = Decimal(str(round(last_close, 4)))
        stop = Decimal(str(round(last_close - self.atr_stop_mult * float(atr_v), 4)))

        # Setup type from the reinforce-only trend read, for sizing/management.
        trend = result.breakdown.get("trend_location", {})
        trend_raw = float(trend.get("raw", trend.get("score", 0.0)))
        setup_type = "dip_in_uptrend" if trend_raw > 0 else "counter_trend_bottom"

        meta = result.breakdown.get("_meta", {})
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
                    "reversal_trigger": result.breakdown.get("reversal_trigger", {}).get("score"),
                    "pivot_proximity": result.breakdown.get("pivot_proximity", {}).get("score"),
                    "trend_raw": round(trend_raw, 4),
                    # Full per-input breakdown for the signal-detail view.
                    "score_breakdown": result.breakdown,
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
        if bars_held >= self.max_holding_days:
            return ExitDecision(reason="time_stop", qty=position.qty)

        # Bound the indicator compute (same trailing-window argument as signals()).
        view = view.tail(self.required_history() + 5)
        close = view["close"]
        last_close = float(close.iloc[-1])

        # Hard stop on entry − ATR multiple (degrades gracefully without full history).
        if len(view) >= self.atr_period + 1:
            atr_v = atr(view["high"], view["low"], close, self.atr_period).iloc[-1]
            if not pd.isna(atr_v):
                stop_level = float(position.avg_cost) - self.atr_stop_mult * float(atr_v)
                if last_close <= stop_level:
                    return ExitDecision(reason="hard_stop", qty=position.qty)

        # Reversal-top exit: the score flipped to a confirmed top.
        result = self.reversal_score(view, as_of)
        if result is not None and float(result.score) <= -self.exit_threshold:
            return ExitDecision(reason="reversal_top", qty=position.qty)

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
