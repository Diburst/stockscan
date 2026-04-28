# TODO

Backlog of deferred features and improvements, with enough context that future-Thomas (or future-Claude) can pick any item up cold. Ordered roughly by impact.

---

## High-impact

### Strategy optimizer (Bayesian search + walk-forward + held-out validation)

**Idea:** A search engine that varies a strategy's parameters across thousands of trials, runs a backtest at each point, and reports the parameter set that maximizes a chosen objective — *with anti-overfitting hygiene baked in by default*. Lets you ask "what's the best `rsi_period` × `atr_stop_mult` × `adx_min` combination for Largecap Rebound on AAPL over 2015–2024?" and get a defensible answer rather than an overfit one.

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

**4. What's optimized.** MVP optimizes parameter VALUES only (RSI period, MACD periods, ATR multiplier, ADX threshold, etc.) — bounded space, easier, less overfit-prone. Optimizing strategy STRUCTURE (which conditions to AND/OR, which indicators to include) is genetic programming territory — much more powerful but much more overfit-prone. Defer to v2.

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

Done well, it answers questions you currently can't: "is `rsi_period=14` actually the best for Largecap Rebound, or is 11 better? How sensitive is performance to `atr_stop_mult`? Is the strategy edge structural or did I luck into one set of parameters?"

Done badly — without walk-forward, without OOS hold-out, without deflated Sharpe — it produces an extremely confident-looking report that says "this strategy makes 200% with 80% win rate" and you blow up your account live-trading parameters that fit one historical sample. Hence why "do not optimize without validation hygiene" should be the first line of the docstring on the runner.

---

### Financial news integration (EODHD /news)

**Idea:** Pull financial news from EODHD's news API and surface it in two places — general market news on the Dashboard, and per-symbol news for everything on the Watchlist. Lets the operator see macro context + news that might explain a watchlisted name's price action without leaving the app.

**Why this matters for swing trading specifically:** earnings beats, FDA approvals, M&A announcements, and macro events (Fed meetings, CPI prints) drive overnight gaps that can blow through ATR-based stops. Already we filter signals near earnings dates; news visibility lets the operator notice *unscheduled* catalysts (lawsuit, executive departure, sector rotation news) on watchlisted names before they hit a position.

#### API surface

EODHD endpoint: `GET /api/news`

Documented query params (verify against current EODHD docs):
- `s` — symbol filter (e.g., `s=AAPL.US`). Optional; when omitted returns broad financial news.
- `t` — topic tag filter (e.g., `t=mergers and acquisitions`, `monetary-policy`, `earnings`). Optional.
- `from` / `to` — ISO date range.
- `limit` — max articles per call (provider-specific cap, typically 1000).
- `offset` — pagination.

Response shape per article: `{date, title, content, link, symbols: [...], tags: [...], sentiment: {polarity, neg, neu, pos}}`. The sentiment field is a per-article score from EODHD's NLP — useful but treat as advisory only.

#### Storage

Cache articles locally so re-renders don't hit the API and so we have history for analysis later.

```sql
CREATE TABLE news_articles (
    article_id      TEXT PRIMARY KEY,           -- hash of (link) or EODHD's id
    published_at    TIMESTAMPTZ NOT NULL,
    title           TEXT NOT NULL,
    snippet         TEXT,                        -- first ~500 chars of content
    link            TEXT NOT NULL,
    source          TEXT,                        -- 'reuters', 'bloomberg', etc.
    sentiment_polarity NUMERIC(5,4),             -- -1..+1
    sentiment_pos    NUMERIC(5,4),
    sentiment_neg    NUMERIC(5,4),
    sentiment_neu    NUMERIC(5,4),
    tags            TEXT[] NOT NULL DEFAULT '{}',
    fetched_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_news_published ON news_articles (published_at DESC);
CREATE INDEX idx_news_tags ON news_articles USING GIN (tags);

-- Many-to-many because articles can mention multiple symbols
CREATE TABLE news_article_symbols (
    article_id  TEXT NOT NULL REFERENCES news_articles(article_id) ON DELETE CASCADE,
    symbol      TEXT NOT NULL,
    PRIMARY KEY (article_id, symbol)
);
CREATE INDEX idx_news_symbols_lookup ON news_article_symbols (symbol, article_id);
```

We **don't store full article content** — keeping a 500-char snippet is plenty for the UI; clicking the link opens the original. Saves storage and avoids any content-rights concerns.

#### Refresh strategy

- **Per-symbol**: refresh news for every watchlisted symbol once a day after market close. ~50 watched names × 1 call = trivial cost.
- **General market**: refresh once a day on the same schedule, using the configured symbol + tag set (see Decisions §1).
- **On-demand**: a "Refresh news" button on the Dashboard / news page for manual pulls.

Schedule via launchd as a daily job that runs after the nightly scan completes (~20:30 ET): `com.stockscan.news-refresh.plist`. Could also fold into the existing nightly-scan job since both run at similar times.

#### UI surfaces

**Dashboard:** new card showing the last 5–10 general-market headlines with publish time, source, and a small sentiment-color dot (green/red/grey). Each item links out to the source.

```
┌──────────────────────────────────────────────────────────────────────┐
│ Market news                                            [Refresh]      │
├──────────────────────────────────────────────────────────────────────┤
│ ● Fed signals two more rate cuts this year       Reuters · 14m ago   │
│ ○ Tech sector leads as semiconductors rebound    Bloomberg · 1h ago  │
│ ● JPMorgan beats Q1 estimates, raises guidance   WSJ · 2h ago        │
│ ...                                                                   │
└──────────────────────────────────────────────────────────────────────┘
```
(● = strong sentiment polarity ±0.5, ○ = mild)

**Watchlist:** add a "news" toggle/button per row that expands to show last 3 headlines for that symbol. Or a compact "news" badge that shows count + most-negative sentiment indicator (red dot if recent negative news).

**Dedicated `/news` page:** chronological feed with filters by symbol, tag, sentiment range, date range. FTS search via Postgres if we extend the `tsvector` pattern from `trade_notes`.

**Per-trade or per-signal news context (stretch):** when looking at a backtest trade or live signal, optionally show news from the few days around the entry. Could explain "why did this trade win/lose" — e.g., a positive earnings surprise on day 3 of a Largecap Rebound entry.

#### Decisions (resolved)

**1. General-market feed scope.** Default-curated mix of (a) major-index symbols, (b) tech/semis/AI bias, (c) macro-event topics. User can edit the curation in Settings.

Default symbol filter (initial seed):
- **Indices/broad ETFs:** `SPY`, `QQQ`, `DIA`, `IWM`
- **Sector ETFs (tech/semis bias):** `XLK`, `SOXX`, `SMH`
- **Mega-cap tech anchors:** `AAPL`, `MSFT`, `NVDA`, `GOOGL`, `META`, `AMZN`, `AMD`, `TSM`, `ASML`

Default topic-tag filter (verify exact strings against EODHD's tag taxonomy at implementation time):
- `monetary-policy` (FOMC, rate decisions)
- `economic-indicators` (CPI, unemployment, GDP, NFP)
- `earnings` (broad earnings season news)
- `artificial-intelligence` (AI-related macro news)

Curation UI: a Settings panel with two text-area fields (one for symbols, one for tags), each populated with the defaults above. User edits → saves to a `news_feed_config` table. The general-market query becomes the union: articles where `symbol IN config.symbols OR tags && config.tags`.

**2. Sentiment treatment.** Surface as advisory metadata only — a small color-coded dot (●/○) next to each headline based on polarity magnitude, no sorting/filtering by sentiment in the MVP. Avoids over-reliance on a noisy NLP score while still making the signal visible at a glance. Tooltip on hover shows the raw polarity number for users who want detail.

**3. Refresh cadence.** Daily, scheduled to run right after the nightly scan completes (~20:30 ET). Manual on-demand refresh button on the Dashboard for ad-hoc pulls. No intraday refreshing in MVP.

**4. Push notifications for high-impact watchlisted-symbol news.** Yes, opt-in. Settings toggle: "Notify on watchlisted-symbol news with |sentiment| > X" (default threshold 0.7, configurable). Uses the existing notify router (email + Discord). Fires once per article — articles already alerted are tracked via a `news_alerted` flag or a join table.

**5. Article retention.** Keep forever. Storage cost is trivial (~1000 articles/day = a few MB/year of metadata). Useful for historical analysis later — could correlate news catalysts with trade outcomes in a Phase 5+ feature.

**6. Source filtering.** Out of scope for MVP. All EODHD-provided sources kept as-is. Revisit if noise becomes a real problem.

#### MVP definition (when this gets built)

- **EODHD provider method** `get_news(symbols=None, tags=None, from_date, to_date, limit=1000)`
- **Migration** (next free number):
  - `news_articles` + `news_article_symbols` (per §Storage)
  - `news_feed_config` — single-row table holding the user-curated `symbols TEXT[]` and `tags TEXT[]` for the general-market feed; seeded with defaults from §Decisions §1
  - `news_alerts_sent` — small (article_id, channel) tracking table so push notifications fire once per article
- **Module** `stockscan/news/` with:
  - `store.py` — CRUD
  - `refresh.py` — bulk pulls (per-watchlist-symbol + general-feed)
  - `alerts.py` — high-sentiment push notifications opt-in
  - `helpers.py` — recent-for-symbol, recent-general, full-text search
- **CLI**:
  - `stockscan refresh news` — pulls watchlist + general feed; idempotent
  - `stockscan news list [--symbol AAPL] [--tag earnings] [--days 7]`
  - `stockscan news search "query"` — Postgres FTS across titles + snippets
- **Web UI**:
  - Dashboard card: last 5–10 general-market headlines with sentiment-color dot, sourced from the union of `news_feed_config.symbols` + `news_feed_config.tags`
  - Watchlist row: expandable per-symbol news (HTMX expand/collapse, last 3 headlines)
  - `/news` dedicated page with chronological feed + filters (symbol, tag, date range, FTS)
  - Settings page section: edit the general-feed `symbols` and `tags` lists; toggle high-sentiment push notifications + threshold
- **Notifications**: opt-in push (email + Discord) for watchlisted-symbol articles with `|sentiment_polarity| ≥ threshold` (default 0.7), fired via the existing notify router, deduplicated by `news_alerts_sent`
- **launchd**: `com.stockscan.news-refresh.plist` running daily at 20:30 ET on weekdays — folded into the existing nightly-scan job is also fine

Estimated effort: ~3–4 days. UI is the bulk of the work (per-symbol expandable sections, the `/news` page, the Settings curation panel); the API integration + storage is straightforward.

---

### Market-regime detector + meta-strategy switching

**Idea:** Detect the broader-market regime (trending vs choppy) and route strategies accordingly. Largecap Rebound and Donchian Trend whip in chop; RSI(2) thrives there. A regime detector would let the scanner *choose which strategies to run* based on current conditions instead of always running everything.

**Concrete shape:**

- New module `stockscan/regime/` with a `detect_regime(as_of)` function that looks at SPY (or some configurable benchmark) and classifies the market as one of:
  - `trending_up` — SPY's ADX(14) > 25 AND close > SMA(200)
  - `trending_down` — ADX > 25 AND close < SMA(200)
  - `choppy` — ADX < 18 (range-bound)
  - `transitioning` — ADX 18–25 (ambiguous)
- Persisted in a `market_regime` table keyed by `(as_of_date)` so backtests + dashboard can query it cheaply.
- `Strategy` ABC gets an optional `applicable_regimes: ClassVar[set[str]]` attribute. Strategies declare which regimes they're active in:
  - `RSI2MeanReversion`: `{"choppy", "trending_up", "transitioning"}` — works in most environments except sustained downtrends
  - `DonchianBreakout`: `{"trending_up", "trending_down"}` — needs a real trend
  - `LargeCapRebound`: `{"trending_up", "transitioning"}` — needs some directional move plus quality-stock recoveries; excluded in pure chop and bear markets
- The `ScanRunner` queries today's regime and skips strategies whose `applicable_regimes` don't include it.
- The dashboard shows the current regime as a badge + a small "Strategies active today" panel.
- The nightly summary email includes the regime in its header.

**Why this is the right answer for the chop problem:** the ADX entry filter on Largecap Rebound is a workaround for a per-strategy chop weakness; the regime detector solves the underlying issue at the portfolio level — *don't run counter-trend strategies in a regime where they don't work*.

**Why deferred:** changes the operational model from "run everything" to "run regime-appropriate." Needs a design discussion about how to handle strategies that already have open positions when the regime flips (close them? hold them? rotate?). Half a day of code + thinking.

---

## Medium-impact

### True historical fundamentals (point-in-time per quarter)

**Current state:** `fundamentals_snapshot` holds the *latest* snapshot per symbol. Backtests apply today's market-cap percentiles to historical bars.

**Problem:** small look-ahead bias on the universe filter. A backtest of 2015 sees today's market cap rankings — names that have grown into the top quintile since 2015 will pass the filter even though they wouldn't have qualified back then. Direction of bias: probably overstates the strategy's apparent performance slightly.

**Fix:** replace `fundamentals_snapshot` with `fundamentals_history (symbol, as_of_date, ...)` — one row per symbol per quarterly earnings reporting. Refresh from EODHD's historical fundamentals endpoint. Strategies use `market_cap_percentile(symbol, as_of)` with a real `as_of` parameter that picks the most-recent snapshot at-or-before that date.

**Why deferred:** a few hundred extra API calls and ~10× the storage for fundamentals (mostly negligible). The current bias is small for 1–3 year windows; gets worse for longer historical backtests. Phase 5 cleanup.

### Signal-detail technical breakdown view

**Current state:** technical scores persist a `breakdown` JSONB with each indicator's raw values + sub-score, but the UI only shows the composite.

**What's missing:** a per-signal page section that renders the breakdown — e.g., "RSI: 28.4 → +0.72; MACD histogram: +0.45 (rising) → +0.55. Composite: +0.64." Useful for understanding *why* a signal got the score it did.

**Where it goes:** signals/detail.html, between the existing "Signal" card and the action buttons. Data is already there in `signals.metadata` joined to `technical_scores.breakdown`.

**Why deferred:** known nice-to-have, called out in v0.7 changelog. ~half day of UI work.

### Cross-symbol "find similar setup" historical search

**Current state:** the base-rate analyzer is per-(strategy, symbol). Computes outcomes for the current symbol's history under the current strategy.

**Idea:** a query like "find every RSI(2) entry across the entire S&P 500 history where RSI(2) ≤ 5 AND close > 200 SMA AND in a bull regime" and aggregate. Lets you base-rate by *setup characteristics* across the universe, not just by symbol+strategy.

**Why deferred:** valuable but additive. The per-symbol view answers "should I take this trade on this name today" which is the most common operator question. Cross-symbol view is a research tool. Phase 5.

### ~~Parameter-sweep UI~~ → subsumed by the strategy optimizer (see "High-impact")

The standalone parameter-sweep idea is replaced by the optimizer entry above. The optimizer's web UI at `/optimizations/{id}` includes the parameter-scatter visualization that was the core of this idea, plus the validation hygiene that a naive sweep would lack.

---

## Smaller items

### Watchlist remove → Dashboard pill auto-flip

When you remove a symbol from `/watchlist`, the Dashboard pill stays "✓ watching" until you reload `/`. Fix: make the Dashboard pill itself an HTMX delete-form so clicking it toggles back to "+ Watch" without a navigate. ~30 minutes.

### Filter-table-by-selected-symbol on Backtest detail

The backtest detail page chart picker focuses ONE symbol's chart, but the trade log below shows ALL symbols. For multi-symbol runs it'd be cleaner to filter the table when a symbol is selected. Add `?show=selected` flag.

### Strategy parameter-edit UI

Pydantic schemas already render in the strategies/detail.html as raw JSON. Wiring up an HTML form generated from the schema would let operators tweak strategy params without editing Python. ~1 day.

### Mobile UI polish on the new backtest chart

Verified responsive at high level but not on-device for the new chart. Lightweight-charts has touch support; needs an iOS Safari + Android Chrome verification pass.

### Note templates configurable

Currently the entry/exit prompts are hardcoded ("Thesis", "What invalidates this?", "What worked", "What I'd change"). Could let the user customize. Minor polish.

### Notification quiet hours

Discord alerts at 2am for a midnight reconciliation pass would be annoying. Add a quiet-hours config that suppresses non-critical alerts outside trading hours. Minor.

### Bulk-refresh fundamentals on a schedule

`stockscan refresh fundamentals` is manual. Adding `infra/launchd/com.stockscan.fundamentals-refresh.plist` to run weekly (Sunday 03:00 ET) would keep the data fresh without manual intervention. Trivial — clone an existing plist.

---

## Phase 4 + 5 (not deferred — just upcoming)

These are the planned next phases per DESIGN.md §11 and aren't really TODOs in the deferred sense. Listed here for completeness:

- **Phase 4 — E*TRADE Integration**: OAuth handshake UI, ETradeBroker against sandbox, integration tests, paper-money rehearsal. ~2 weeks.
- **Phase 5 — Hardening**: Reconciliation drift loop, error handling refinement, performance reporting, weekly journal export.

---

## Done — kept here for record / context

These items started as "nice to have someday" and have shipped:

- ~~Watchlist with price-target alerts~~ ✓ shipped
- ~~Technical confirmation score (per-strategy, signed)~~ ✓ shipped
- ~~Fundamentals snapshot layer + market_cap_percentile~~ ✓ shipped
- ~~Largecap Rebound strategy (counter-trend on quality)~~ ✓ shipped
- ~~Bulk EOD endpoint for fast daily refresh~~ ✓ shipped
- ~~Per-symbol price chart with entry/exit markers in backtest detail~~ ✓ shipped
- ~~R-multiple ("return on risk") on backtest trades~~ ✓ shipped
- ~~Mobile-first responsive UI~~ ✓ shipped
- ~~Strategy plugin system with auto-discovery~~ ✓ shipped
- ~~Beginner-friendly strategy manuals~~ ✓ shipped (RSI(2), Donchian; Largecap Rebound's manual is still the placeholder)
- ~~Custom SQL migration runner (replaced Alembic)~~ ✓ shipped
