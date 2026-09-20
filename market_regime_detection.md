# Market Regime Layer — Design Note

**Status:** implemented (`stockscan.regime`, migration 0025) · **Date:** 2026-09

The regime layer is not a classifier. It is two portfolio-level controls and one
breaker, each doing one job the evidence supports, wired identically into the
live scanner and the backtest engine. This note records what the controls are,
why each exists, the exact rules and constants, and the backtests still owed.

---

## 1. The controls

| Control | Job | Evidence type |
|---|---|---|
| **Trend gate** | keep new entries out of bear markets | drawdown reduction, not return prediction |
| **Volatility scalar** | shrink position size when realized vol is high, for the strategies that lose there | Sharpe improvement via risk control |
| **Credit-stress breaker** | refuse new longs during a credit tail event | rare-tail override, not a signal |

They are deliberately not blended. Each has its own units, its own failure mode,
and — for the vol scalar — a different sign per strategy family.

### 1.1 Trend gate

SPY close versus its 200-day simple moving average, with hysteresis: the gate
opens after `TREND_DWELL = 3` consecutive closes above the SMA and closes after 3
consecutive closes below. The first bar with a defined SMA seeds the state from
its own side. A closed gate blocks **new long entries only**; open positions run
their own exits. The dashboard shows `days_on_side`, the dwell counter.

*Why.* The 10-month / 200-day index filter is the one timing rule with a
consistent out-of-sample record, and what it buys is a shallower drawdown
distribution rather than a higher mean return: Faber (2007) across asset classes;
ap Gwilym, Clare, Seaton & Thomas (2010) on 32 international equity markets;
Zakamulin (2014, 2017) showing the return advantage is mostly a bear-market
effect and unstable out of sample while the drawdown reduction holds. Daily
evaluation without a band is the version practitioners warn against (whipsaw
around the line); the 3-close dwell is the cheapest band that removes single-day
flips without adding a lag the monthly rule would not have.

### 1.2 Volatility scalar

Realized SPY volatility — 20-day standard deviation of daily log returns,
annualized — percentile-ranked over the trailing 252 bars. When the rank is in
the top tercile (`VOL_HIGH_RANK = 2/3`) the scalar is
`clip(TARGET_VOL / realized, VOL_SCALAR_FLOOR, 1.0)` with `TARGET_VOL = 0.16`
and floor `0.5`; otherwise it is `1.0`. It never scales above 1: a long-only
retail book does not lever into calm markets.

The scalar multiplies position size only for strategies that declare
`sizes_down_in_high_vol = True`. Momentum does; mean reversion does not.

*Why.* Moreira & Muir (2017) show that scaling exposure by the inverse of
recent realized variance raises Sharpe ratios across factor portfolios, with
the gain concentrated in avoiding high-variance periods. Barroso & Santa-Clara
(2015) and Daniel & Moskowitz (2016) show the momentum-specific version: momentum
crashes come in high-vol rebounds after bear markets, and scaling momentum by its
realized variance roughly doubles its Sharpe and removes the worst months.
Harvey, Hoyle, Korgaonkar, Rattray, Sargaison & Van Hemert (2018) confirm the
benefit is largest for equities and equity-like strategies and smallest for
assets without leverage-effect asymmetry. Cederburg, O'Doherty, Wang & Yan
(2020) are the caution: across 103 anomalies, vol management does not
systematically beat the unmanaged version in real time — so the scalar here is
a risk control with a dead band (Bongaerts, Kang & van Dijk 2020 show most of
the benefit survives with size changes only in the extreme-vol states), not a
return enhancer, and it only scales *down*.

The sign matters. Nagel (2012) shows short-term reversal profits rise with VIX —
reversal is compensation for supplying liquidity, and that compensation is
highest when volatility is high. Shrinking RSI(2) positions in high vol would
cut the strategy exactly where it pays best; hence the per-strategy opt-in.

### 1.3 Credit-stress breaker

HY OAS (FRED `BAMLH0A0HYM2`, ICE BofA US High Yield OAS) in the top 15% of its
trailing 252 observations **and** higher than 5 observations earlier. While it
fires, new longs are refused. It is aligned to the SPY calendar by forward-fill
(FRED publishes with a lag).

*Why.* Gilchrist & Zakrajšek (2012) show credit spreads — specifically the
excess bond premium — lead real activity and equity drawdowns at a multi-month
horizon. That makes the *level* a recession indicator, not a swing-horizon
timing signal, and it is ~0.8 correlated with VIX, so as a continuous input it
is redundant with the vol scalar. The rising-and-extreme conjunction keeps only
the tail event, where refusing new longs for a few weeks costs little.

---

## 2. What was retired, and why

The previous layer (methodology version 2) blended four components — VIX
percentile, SMA(200) slope, RSP/SPY breadth and HY OAS percentile — into a
40/25/20/15 composite, carried an ADX-based four-way label, and let each strategy
declare an affinity map that multiplied size by `affinity × (0.5 + 0.5 ×
composite)`. It was dissolved at the 2026-09 canon review because:

- **A composite hides its sign.** Momentum wants to size down in high vol;
  reversal wants to size up. One scalar cannot serve both, and the affinity maps
  were a per-strategy patch over that fact.
- **Breadth added nothing the trend gate did not.** RSP/SPY and %-above-SMA200
  are collinear with the index trend at the daily horizon, and there is no
  time-series evidence that breadth improves on the 200-day rule for entry
  timing.
- **Credit level is a recession predictor**, months ahead, and ~0.8 correlated
  with VIX. It stays only as the breaker.
- **ADX regime labels** never had an evidentiary basis for entry gating; the
  label drove nothing once sizing moved to the composite, and it went with it.
- **The backtest engine only applied the affinity.** The composite, the stress
  override and the label were live-only, so backtests measured a different
  system from the one trading. `regime_frame` fixes that structurally.

VIX itself is gone as an input: realized SPY vol is computed from bars the
system already holds, needs no `.INDX` fetch, and is what the vol-scaling
literature uses.

---

## 3. Rules and constants (`stockscan/regime/rules.py`)

```
TREND_SMA_WINDOW              = 200
TREND_DWELL                   = 3       consecutive closes before the gate flips

VOL_WINDOW                    = 20      bars in the realized-vol estimate
VOL_RANK_WINDOW               = 252     trailing year for the percentile rank
VOL_HIGH_RANK                 = 2/3     top tercile → scaling active
TARGET_VOL                    = 0.16    annualized, ≈ SPY long-run realized
VOL_SCALAR_FLOOR              = 0.5

CREDIT_RANK_WINDOW            = 252
CREDIT_STRESS_RANK_THRESHOLD  = 0.85
CREDIT_STRESS_LOOKBACK        = 5       observations
```

`regime_frame(spy_close, hy_oas) -> DataFrame` returns, per SPY bar: `sma200`,
`sma200_slope_20d`, `trend_gate_open`, `days_on_side`, `realized_vol`,
`vol_pct_rank`, `vol_scalar`, `hy_oas`, `hy_oas_pct_rank`, `credit_stress_flag`.
Warmup bars are NaN (vol) or `False` (gate, flag); the credit columns are
NaN/`False` when `hy_oas` is `None`.

Derived state:

- `block_new_longs = credit_stress_flag or not trend_gate_open`
- `vol_multiplier = vol_scalar` (1.0 when NaN)
- display label: `credit_stress` if the flag fires, else `risk_on` / `risk_off`
  by the gate.

**No look-ahead.** Every quantity is a trailing rolling window or a forward
state machine over the series; recomputing on a truncated copy of the input
reproduces the live value at the truncation point. `tests/test_regime_rules.py`
holds the truncation-invariance property tests.

---

## 4. One rule set, two readers

- **Live** — `detect_regime(as_of)` (`regime/detect.py`) pulls two years of SPY
  closes and the HY OAS series, runs `regime_frame`, persists the last row to
  `market_regime` (`regime/store.py`, `methodology_version = 3`) and returns it.
  Rows are cached by date; rows written under an older methodology are
  recomputed on first touch. The nightly job forces a recompute after the bars
  and macro refresh and before any scan. The scanner reads `block_new_longs`
  and `vol_multiplier`; a missing row sizes neutrally with a warning.
- **Backtest** — `BacktestEngine._regime_today` evaluates `regime_frame` once
  per run over SPY bars from 800 calendar days before `start_date` (enough to
  seed the gate and fill the 252-bar rank) plus the HY OAS series, then indexes
  the frame by day. Missing SPY bars disable the controls for the run with one
  logged warning rather than silently emptying it.
- **Sizing** — both readers hand the scalar to
  `stockscan.risk.sizer.size_for_strategy`, which applies it only when the
  strategy's `sizes_down_in_high_vol` is true.

Because the two readers share the function, a backtest measures the gate, the
scalar and the breaker exactly as they trade.

---

## 5. Settling backtests still to run

All on the point-in-time S&P 500, 5 bp slippage, 2010–2026, walk-forward with
the last two years held out (see `TODO.md` for the full list including the
per-strategy ablations):

1. **Trend gate with and without the dwell**, and against no gate at all — the
   expected result is a similar CAGR with a materially shallower max drawdown;
   the dwell should cut the number of gate flips without changing the drawdown
   result.
2. **Vol scalar on / off for momentum** — the metric of interest is max drawdown
   and the worst-month distribution, not CAGR.
3. **Vol scalar forced on for RSI(2)** — expected to *reduce* returns, confirming
   the opt-out sign.
4. **Credit breaker on / off** — expected to fire rarely (2011, 2015–16, 2020,
   2022) and to change little except in those windows.

---

## References

- Faber, M. (2007). "A Quantitative Approach to Tactical Asset Allocation." *Journal of Wealth Management*.
- ap Gwilym, O., Clare, A., Seaton, J. & Thomas, S. (2010). "Price and Momentum as Robust Tactical Approaches to Global Equity Investing." *Journal of Investing*.
- Zakamulin, V. (2014). "The Real-Life Performance of Market Timing with Moving Average and Time-Series Momentum Rules." *Journal of Asset Management*; and *Market Timing with Moving Averages* (2017).
- Moreira, A. & Muir, T. (2017). "Volatility-Managed Portfolios." *Journal of Finance* 72(4).
- Barroso, P. & Santa-Clara, P. (2015). "Momentum Has Its Moments." *Journal of Financial Economics* 116(1).
- Daniel, K. & Moskowitz, T. (2016). "Momentum Crashes." *Journal of Financial Economics* 122(2).
- Harvey, C., Hoyle, E., Korgaonkar, R., Rattray, S., Sargaison, M. & Van Hemert, O. (2018). "The Impact of Volatility Targeting." *Journal of Portfolio Management* 45(1).
- Cederburg, S., O'Doherty, M., Wang, F. & Yan, X. (2020). "On the Performance of Volatility-Managed Portfolios." *Journal of Financial Economics* 138(1).
- Bongaerts, D., Kang, X. & van Dijk, M. (2020). "Conditional Volatility Targeting." *Financial Analysts Journal* 76(4).
- Nagel, S. (2012). "Evaporating Liquidity." *Review of Financial Studies* 25(7).
- Gilchrist, S. & Zakrajšek, E. (2012). "Credit Spreads and Business Cycle Fluctuations." *American Economic Review* 102(4).
