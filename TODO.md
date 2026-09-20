# TODO

Backlog of deferred features and improvements, with enough context that future-Thomas (or future-Claude) can pick any item up cold. Ordered roughly by impact.

---

## Settling backtests (2026-09 canon review)

The canon review reduced the book to `rsi2_meanrev` and `momentum_52w_high` and
settled the regime layer as a trend gate, a vol scalar and a credit
breaker. Each of the calls below was made on the literature; these runs confirm
them on our own data. Common setup for every run: point-in-time S&P 500
universe, 5 bp slippage, 2010-01-01 → today, walk-forward with the last two
years held out. Each ablation is a knob edit + version bump on a branch and a
`stockscan backtest run`; record the `knobs_hash` with the run note.

**RSI(2) pullback**
- [ ] With and without the sector-relative ranking (`sector_min_return` off, rank by raw RSI instead of `idiosyncratic_drop`). Expect: sector conditioning improves expectancy per trade and drawdown in 2015–16 and 2022.
- [ ] With and without a 3×ATR(14) price stop added to `exit_rules`. Expect: the stop lowers returns more than it lowers drawdown (Kaminski & Lo). If it does not, the no-stop decision is reopened.

**52-week-high momentum**
- [ ] Rank-based entry (top of the eligible list per weekly review) versus a plain `min_closeness = 0.95` threshold with no ranking. Expect: ranking wins on turnover and on drawdown; similar CAGR.
- [ ] 15% stop versus SMA(100)-break only. Expect: the stop roughly doubles Sharpe by cutting crash months (Han, Zhou & Zhu); if it merely adds turnover, drop it.

**Regime controls**
- [ ] Trend gate with the 3-close dwell, without it, and no gate. Expect: similar CAGR, materially shallower max drawdown with the gate; the dwell cuts flips without changing the drawdown result.
- [ ] Vol scalar on/off for momentum — report max drawdown and the worst-month distribution, not just CAGR. Also run the scalar forced on for RSI(2) to confirm it hurts there.

Results go into the strategy `manual` (one paragraph each) and, where a call is
reversed, into a version bump with the reason in the module docstring.

---

## High-impact

### Strategy optimizer (Bayesian search + walk-forward + held-out validation)

**Idea:** A search engine that varies a strategy's parameters across thousands of trials, runs a backtest at each point, and reports the parameter set that maximizes a chosen objective — *with anti-overfitting hygiene baked in by default*. Lets you ask "what's the best `rsi_entry` × `max_holding_bars` × `sector_min_return` combination for RSI(2) over 2015–2024?" and get a defensible answer rather than an overfit one. Knobs are class constants, so a trial is an instance with overridden attributes, not a parameter object.

**The risk to call out loudly in the docs:** this is the single most landmine-laden feature in retail quant trading. With enough degrees of freedom and a single sample, an optimizer will *always* find parameters that beat the benchmark on that sample — even on pure random walks. That's a statistical certainty, not a bug. The optimizer's job isn't to "find the best parameters"; it's to **find robust parameters and honestly report how robust they are**.

**Default objective MUST NOT be total return.** Total return alone rewards reckless one-shot bets. Defaults should be Sharpe ratio or expectancy-in-R, both of which penalize variance. Total return remains available as an objective but with a warning in the CLI/UI.

#### Six design dimensions

**1. Search backend.** Three reasonable choices, all behind the same `SearchStrategy` ABC:

- **Random search** — sample random points from the parameter space. Surprisingly competitive with grid for any space ≥3 dimensions; trivial to parallelize. Works without any new deps. *Ship this first.*
- **Bayesian optimization via Optuna** — model the objective surface, propose informed next trials. ~5–10× more sample-efficient than random for typical strategy spaces. Adds `optuna` as an optional dependency (`[optimizer]` extra). *Ship in MVP alongside random.*
- **Grid search** — enumerate every combination. Useful for small spaces and as a "complete sweep" sanity check. Simple to implement.

MVP carries random + Optuna; grid as a v2 add-on. Switching is a CLI flag.

**2. Objective function.** Library of metrics:

- `sharpe` (canonical risk-adjusted return; default)
- `sortino` (penalize downside only)
- `profit_factor`
- `expectancy_r` (per-trade R-multiple expectancy; aligns with the existing R column)
- `composite` (e.g., `sharpe × profit_factor / max_drawdown_pct`)
- `total_return` (allowed but flagged with a warning)

The objective module is small (~50 lines) — each function takes the `BacktestResult` and returns a float to maximize.

**3. Validation methodology — load-bearing.**

- **Walk-forward analysis (default ON):** split the time range into N consecutive windows. Optimize on window 1, test on window 2; optimize on windows 1+2, test on window 3; etc. Report per-window stability of "best" parameters. The single most important anti-overfitting tool.
- **Held-out reservation (default ON):** lock the most recent 12–24 months as untouchable. Optimizer never sees this window. Final report: "best on training data, performance on held-out data" — typically 30–50% Sharpe degradation; bigger gaps mean the optimizer overfit.
- **Cross-symbol robustness (optional):** optimize on a basket (AAPL, MSFT, GOOG, JPM, JNJ), validate on names not in the basket. Strongest robustness signal but compute-heavy.
- **Single-window mode** — possible but flagged in CLI and UI as "exploratory only — DO NOT use these parameters live without walk-forward validation."

**4. What's optimized.** MVP optimizes knob VALUES only (RSI entry level, holding period, closeness threshold, stop percentage, etc.) — bounded space, easier, less overfit-prone. Optimizing strategy STRUCTURE (which conditions to AND/OR, which indicators to include) is genetic programming territory — much more powerful but much more overfit-prone. Defer to v2.

**5. Persistence + reproducibility.** Every run is an artifact:

- `optimization_runs` table: `run_id`, `strategy_name`, `strategy_version`, `symbol(s)`, `search_method`, `n_trials`, `objective`, `walk_forward_windows`, `holdout_start_date`, `best_params_json`, `validation_report_json`, `created_at`, `note`
- `optimization_trials` table: `trial_id`, `run_id`, `trial_number`, `params_json`, `in_sample_metrics_json`, `out_of_sample_metrics_json`, `walk_forward_per_window_json`
- All trials retained so you can plot the search trajectory and visually verify whether the "best" parameters are in a stable plateau vs a single noise spike

**6. Anti-overfitting hygiene baked into the report.** Final output for every recommended parameter set:

- **Walk-forward stability score** — how consistent is "best" across windows? (e.g., correlation of in-window-best params)
- **Out-of-sample degradation** — Sharpe in-sample / Sharpe out-of-sample. Anything below 0.5 is suspicious.
- **Deflated Sharpe ratio** — corrects for multiple-comparison bias given `n_trials` (López de Prado, 2014). Critical for honest reporting.
- **Objective surface plot** — a 2D heatmap showing the top two parameters' impact. Lets the operator visually confirm whether best params are in a stable plateau or a noise spike.
- **Parameter robustness check** — perturb each "best" param by ±10% and check whether performance degrades smoothly (good) or falls off a cliff (overfit).

Without these, the optimizer is a footgun.

#### Architecture sketch

```
src/stockscan/optimizer/
├── search/
│   ├── base.py            ← SearchStrategy ABC (suggest_next, observe)
│   ├── random.py          ← MVP
│   ├── bayes.py           ← MVP, wraps Optuna
│   └── grid.py            ← v2
├── objective.py           ← Sharpe, Sortino, expectancy_r, composite (~50 lines)
├── walkforward.py         ← splits time, runs optimize-then-test
├── deflated_sharpe.py     ← multiple-comparison correction
├── reporter.py            ← validation report + objective surface plot
└── runner.py              ← orchestrator: search × walkforward × reporter

migrations/0008_optimization.sql
  optimization_runs + optimization_trials (see §5 above)

CLI:
  stockscan optimize run STRATEGY \
       --symbol AAPL --from 2010-01-01 \
       --objective sharpe \
       --search bayes --trials 100 \
       --walk-forward 4 \
       --holdout-months 18
  stockscan optimize list
  stockscan optimize show RUN_ID

Web:
  /optimizations           (list of runs)
  /optimizations/{id}      (best params + validation report + trial scatter)

pyproject.toml:
  [project.optional-dependencies]
  optimizer = ["optuna>=4.0"]
```

#### Open questions for the implementor

1. **Optimize per-symbol, per-basket, or universe-wide?** Per-symbol is what the user requested; basket and universe are more robust but more compute. Probably support all three with `--symbol` (single), `--basket` (named set), or omitted (full S&P 500).
2. **Default objective?** Sharpe vs expectancy-in-R. Sharpe is canonical; expectancy-in-R aligns with our R-multiple infrastructure.
3. **Walk-forward windows: rolling vs anchored?** Rolling = each window slides forward (e.g., 1-year train, 6-month test, slide 6 months). Anchored = expanding training window (train on 1 yr, test month 13–24; train on 2 yr, test month 25–36; ...).
4. **Compute strategy.** Serial trials are slow (each backtest takes ~5–30 sec on a single symbol). Multiprocess pool is the obvious answer but blows up memory for the bars cache. Async via existing infrastructure is tighter but harder. Default to multiprocess with a configurable worker count.
5. **Surface deflated Sharpe in the headline metric?** Honest but technical. Probably yes, with a tooltip explaining the correction.
6. **When does structure-optimization (v2) become viable?** Likely never with single-symbol scope (even more overfit-prone than param values). Could be useful at universe-wide scope. Genetic programming has well-documented overfitting failures in finance.

#### MVP definition (when this gets built)

- Search: random + Bayesian (Optuna)
- Validation: walk-forward (default 4 windows) + held-out reservation (default last 18 months)
- Objective library: Sharpe, Sortino, expectancy_r, profit_factor, composite, total_return (with warning)
- Persistence: optimization_runs + optimization_trials tables
- CLI: `stockscan optimize run|list|show`
- Web: `/optimizations/{id}` showing best params, validation report, parameter scatter plot
- Hygiene baked into the report: walk-forward stability, OOS degradation, deflated Sharpe, parameter perturbation check

Estimated effort: ~1 week including all of the anti-overfitting hygiene. Without the hygiene it's 2 days; with it the result is actually trustworthy.

#### Why this is high-impact (and dangerous)

Done well, it answers questions you currently can't: "is `rsi_entry = 10` actually the best for RSI(2), or is 5 better? How sensitive is momentum to `stop_pct`? Is the strategy edge structural or did I luck into one set of knobs?"

Done badly — without walk-forward, without OOS hold-out, without deflated Sharpe — it produces an extremely confident-looking report that says "this strategy makes 200% with 80% win rate" and you blow up your account live-trading parameters that fit one historical sample. Hence why "do not optimize without validation hygiene" should be the first line of the docstring on the runner.

---

---

## Medium-impact

### True historical fundamentals (point-in-time per quarter)

**Current state:** `fundamentals_snapshot` holds the *latest* snapshot per symbol; `fundamentals_history` (migration 0023) holds point-in-time shares outstanding for the cap-weighted composites, but not the other fields.

**Problem:** the sector map behind the composites is the frozen latest snapshot, so a symbol that changed sector is composited under its current sector for its whole history. No strategy filters on market cap at scan time any more, so the universe-filter look-ahead that motivated this item is gone; what remains is the sector-map drift.

**Fix:** extend `fundamentals_history` with sector/industry per period (EODHD's historical fundamentals payload carries them) and have the composite builder use the sector as of each date.

**Why deferred:** sector reclassifications are rare in the S&P 500; the effect on a sector-relative rank is second-order. Phase 5 cleanup.

### Cross-symbol "find similar setup" historical search

**Current state:** the base-rate analyzer is per-(strategy, symbol). Computes outcomes for the current symbol's history under the current strategy.

**Idea:** a query like "find every RSI(2) entry across the entire S&P 500 history where RSI(2) ≤ 5 AND close > 200 SMA AND in a bull regime" and aggregate. Lets you base-rate by *setup characteristics* across the universe, not just by symbol+strategy.

**Why deferred:** valuable but additive. The per-symbol view answers "should I take this trade on this name today" which is the most common operator question. Cross-symbol view is a research tool. Phase 5.

---

## Smaller items

### On-device verification of the Compose build and the background Refresh

Left over from the 2026-06 hardening pass, which ran in a sandbox without a Docker daemon: run `docker compose build` on the Mac mini and click through the Refresh UX (POST `/refresh` → step polling → dashboard reload) on a phone.

### Filter-table-by-selected-symbol on Backtest detail

The backtest detail page chart picker focuses ONE symbol's chart, but the trade log below shows ALL symbols. For multi-symbol runs it'd be cleaner to filter the table when a symbol is selected. Add `?show=selected` flag.

### Mobile UI polish on the new backtest chart

Verified responsive at high level but not on-device for the new chart. Lightweight-charts has touch support; needs an iOS Safari + Android Chrome verification pass.

### Note templates configurable

Currently the entry/exit prompts are hardcoded ("Thesis", "What invalidates this?", "What worked", "What I'd change"). Could let the user customize. Minor polish.

### Notification quiet hours

Discord alerts at 2am for a midnight reconciliation pass would be annoying. Add a quiet-hours config that suppresses non-critical alerts outside trading hours. Minor.

## Phase 4 + 5 (not deferred — just upcoming)

These are the planned next phases per DESIGN.md §11 and aren't really TODOs in the deferred sense. Listed here for completeness:

- **Phase 4 — E*TRADE Integration**: OAuth handshake UI, ETradeBroker against sandbox, integration tests, paper-money rehearsal. ~2 weeks.
- **Phase 5 — Hardening**: Reconciliation drift loop, error handling refinement, performance reporting, weekly journal export.

---

## Done — kept here for record / context

These items started as "nice to have someday" and have shipped:

- ~~Watchlist with price-target alerts~~ ✓ shipped
- ~~Fundamentals snapshot layer + market_cap_percentile~~ ✓ shipped
- ~~Bulk EOD endpoint for fast daily refresh~~ ✓ shipped
- ~~Per-symbol price chart with entry/exit markers in backtest detail~~ ✓ shipped
- ~~R-multiple ("return on risk") on backtest trades~~ ✓ shipped
- ~~Mobile-first responsive UI~~ ✓ shipped
- ~~Strategy plugin system with auto-discovery~~ ✓ shipped
- ~~Beginner-friendly strategy manuals~~ ✓ shipped (both strategies)
- ~~Custom SQL migration runner (replaced Alembic)~~ ✓ shipped
- ~~Financial news integration (EODHD /news)~~ ✓ shipped (feed + on-demand reader; the `/news` page and sentiment push alerts were not built)
- ~~Market-regime layer~~ ✓ shipped as trend gate + vol scalar + credit breaker (see `market_regime_detection.md`) — the "run only regime-appropriate strategies" idea was replaced by gating new entries and per-strategy vol scaling
- ~~Volatility-managed sizing overlay (Moreira-Muir)~~ ✓ shipped as the regime vol scalar, applied per strategy via `sizes_down_in_high_vol`
- ~~Weekly fundamentals refresh on a schedule~~ ✓ shipped (`infra/crontab`, Sun 03:00 ET)
- ~~Point-in-time shares outstanding~~ ✓ shipped (`fundamentals_history`, migration 0023)
