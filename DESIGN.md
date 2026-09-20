# Personal Stock Trading App — Design Document

**Author:** Thomas
**Status:** v1.0 — Phases 0–3 implemented; strategy canon and regime layer settled at the 2026-09 review
**Date:** 2026-09-19

This document describes the system as it runs today. Earlier revisions carried
per-version changelogs; those were dropped at the 2026-09 canon review and live in
git history. The decision record behind that review is
`market_regime_detection.md` (regime layer) and the 2026-09 canon review notes
(strategy roster).

## 1. Goals

Build a personal swing-trading toolkit that:

1. **Scans** the S&P 500 nightly for technical setups across one mean-reversion and one trend-following strategy.
2. **Backtests** each strategy with realistic execution assumptions and survivorship-bias-corrected data.
3. **Manages** the lifecycle of every position from signal → order → fill → exit, with full P&L attribution.
4. **Executes** trades through E*TRADE behind a broker abstraction that lets us swap providers (or run with no broker at all) without touching strategy code.
5. **Runs** unattended on a home server, with a local web UI for monitoring and manual interaction.

### Non-Goals (v1)

- Intraday / day trading (requires a different data tier and event loop).
- Options, futures, FX, international equities.
- Multi-user / multi-tenant (single user, single account).
- Machine-learning models or alternative data.
- Mobile app (web UI is responsive enough).
- Tax optimization (we'll log enough data to do it externally).

---

## 2. Locked Decisions Summary

| Area | Decision |
|------|---------|
| Trading horizon | Swing (days–weeks), end-of-day bars, signals at close → orders at next open |
| Universe | S&P 500 (with historical constituents for survivorship correction) |
| Strategies | One mean-reversion (`rsi2_meanrev`), one trend-following (`momentum_52w_high`) — see §6. Nothing else is in the book |
| Capital | `STOCKSCAN_STARTING_EQUITY` (default $100,000) until the first `equity_history` row; integer shares only |
| Risk per trade | Declared on the strategy: momentum risks 0.75% of equity against its 15% stop; RSI(2) carries no stop and takes a fixed 10% of equity per position |
| Earnings filter | Skip both MR and TF entries within 5 trading days of next reported earnings |
| Brokerage | E*TRADE first, behind a `Broker` abstraction; "Suggestion Mode" is a first-class no-broker output |
| Accounts | Single account v1; schema includes `account_id` everywhere for future multi-account |
| Tax lots | Specific-lot tracking; user selects lots at exit time; FIFO as default suggestion |
| Data provider | EODHD All-In-One ($99.99/mo) — bundles EOD + intraday + fundamentals + historical S&P 500 constituents (see §7) |
| Tech stack | Python 3.12+, FastAPI, HTMX, **PostgreSQL 16 + TimescaleDB** (Docker), SQLAlchemy 2 + raw-SQL migrations, pandas/NumPy, hand-rolled indicators (`stockscan.indicators`) |
| Storage philosophy | Single source of truth: Postgres holds everything (bars, transactional). TimescaleDB hypertable for bars with compression + continuous aggregates. Optional nightly Parquet export for portability. |
| Hosting | Apple Silicon Mac mini (launchd) or any Docker host (supercronic scheduler); web UI on LAN over HTTPS |
| Notifications | Email (Postmark) + Discord webhook |

---

## 3. System Architecture

```
                    ┌─────────────────────────────────────────┐
                    │            Scheduler (launchd)          │
                    │  20:00 ET: refresh bars, scan, notify   │
                    │  09:25 ET: place pending orders         │
                    │  16:05 ET: reconcile positions, mark    │
                    └────┬────────────────────┬───────────────┘
                         │                    │
                ┌────────▼─────┐      ┌───────▼────────┐
                │  Data Layer  │      │ Position Mgr   │
                │ (provider →  │      │ (lifecycle,    │
                │  TimescaleDB │      │  reconcile,    │
                │  hypertable) │      │  exit logic)   │
                └────┬─────────┘      └───────▲────────┘
                     │                        │
                     ▼                        │
                ┌─────────────┐         ┌─────┴──────┐
                │  Universe   │ ──────► │  Scanner   │ ─► signals
                │  Manager    │         │ (strategies)│
                └─────────────┘         └─────┬──────┘
                                              │
                          ┌───────────────────┼─────────────────┐
                          ▼                   ▼                 ▼
                  ┌───────────────┐  ┌───────────────┐  ┌──────────────┐
                  │  Backtester   │  │   Sizer +     │  │   Web UI     │
                  │ (event-driven │  │   Risk Engine │  │ (FastAPI +   │
                  │  shares code  │  └──────┬────────┘  │   HTMX)      │
                  │  with live)   │         │           └──────────────┘
                  └───────────────┘         ▼
                                    ┌───────────────┐
                                    │ Broker (ABC)  │
                                    └───────┬───────┘
                                            │
                       ┌────────────────────┼────────────────────┐
                       ▼                    ▼                    ▼
                ┌─────────────┐     ┌─────────────┐     ┌──────────────────┐
                │  ETradeBroker│    │ AlpacaBroker│     │ SuggestionBroker │
                │  (OAuth 1.0a)│    │  (REST)     │     │ (no-op, logs &   │
                └─────────────┘     └─────────────┘     │  emails ideas)   │
                                                        └──────────────────┘
                                    ┌──────────────┐
                                    │  PaperBroker │ ◄── used by backtester
                                    │ (sim fills)  │     and dry-run mode
                                    └──────────────┘

                 PostgreSQL 16 + TimescaleDB (Docker) — single source of truth
                   ├── transactional: accounts, signals, orders, lots, lot_sales, equity_history
                   └── time-series: bars (hypertable, compressed), bars_weekly (continuous agg)
                 Optional nightly Parquet export ◄── for Jupyter / DuckDB / portability
```

---

## 4. Module Breakdown

### 4.1 Data Layer (`stockscan.data`)

**Responsibilities:** Pull bars from the provider, persist to the local TimescaleDB store, expose a clean `get_bars(symbol, start, end)` API. Handle splits/dividends (adjusted prices). Build the local database up over time so we never lose history if the provider relationship changes.

**Components:**
- `providers/eodhd.py` — REST client, rate-limit aware, retries on 5xx.
- `providers/base.py` — `DataProvider` ABC (so we can swap to Polygon/Tiingo later without changing callers).
- `store.py` — Postgres/TimescaleDB persistence layer. `upsert_bars(df)` is the single ingest path (idempotent on `(symbol, bar_ts)`); `get_bars(symbol, start, end)` reads back. Every fetch from the provider goes through `upsert_bars`, so the local DB grows monotonically.
- `corporate_actions.py` — Track splits/dividends in their own table; verify locally-stored adjusted prices reconcile with provider on each refresh (sanity check). Splits trigger a re-adjust of historical rows (rare but must be correct).
- `backfill.py` — One-shot tool to bulk-load history from EODHD on first run (typically 16+ years × S&P 500 = ~2M rows, finishes in minutes with batched requests).

**Key invariants:**
- The `bars` table stores **both** raw and adjusted prices: `open / high / low / close / volume` are the unadjusted quotes from the exchange, and `adj_close` is EODHD's split- and dividend-adjusted close. Storing both lets us answer "what did this stock actually trade for on date X" (tax-lot accounting, live-order routing) and "what does the historical price series look like on a continuous total-return basis" (indicators, backtests) from the same row.
- `get_bars(symbol, start, end)` returns the **adjusted** view by default (`adjust=True`): OHLCV is rescaled by the per-bar ratio `adj_factor = adj_close / close`, with `close` set exactly to `adj_close` and `volume` divided by the factor (a 4:1 split quadruples shares). The unadjusted source values are preserved in `open_raw / high_raw / low_raw / close_raw / volume_raw`. Pass `adjust=False` to opt out and read raw bars verbatim. Edge cases (NULL `adj_close`, `close <= 0`) fall back to `adj_factor = 1.0` so the returned frame never contains NaN or inf from the adjustment step.
- All historical analysis (indicators, returns, backtest entry/exit, charts, the regime frame) reads from `get_bars()` and so is **automatically split- and dividend-corrected** without per-strategy code. Around AAPL's 2014 7:1 and 2020 4:1 splits, this eliminates the ~75% fake one-day drawdowns that would otherwise pop entries, trip stops, and blow out ATR for weeks.
- Bar timestamps are `TIMESTAMPTZ` set to **16:00 America/New_York** for daily bars (ready for intraday later, where the timestamp is the bar's open/close depending on convention).
- Ingest is **idempotent**: re-fetching the same date for the same symbol updates the row in place but never duplicates. Indispensable for retries and corporate-action re-adjustments.
- The DB is the source of truth; the provider is just a refresh source. Backtests, scans, and analytics all read from the DB, never directly from the API.

**Why this matters:** building up local history over time is its own asset. If EODHD changes pricing, sunsets an endpoint, or you switch providers, your accumulated bars (corporate-action-adjusted, validated, joined with earnings and corporate actions) stay yours. The local DB also enables expensive ad-hoc backtesting research without burning provider quota.

### 4.2 Universe Manager (`stockscan.universe`)

**Responsibilities:** Maintain the active S&P 500 list and (critically) the **historical membership** for backtesting.

- Live universe: refresh weekly from EODHD's S&P 500 constituents endpoint.
- Historical membership: pull `HistoricalTickerComponents` from EODHD; persist to the `universe_history` table `(symbol, joined_date, left_date)`.
- Exclusion overrides: user-editable YAML allowlist/denylist (e.g., skip a name through earnings).

### 4.3 Scanner (`stockscan.scan`)

**Responsibilities:** Apply each strategy's entry rules to today's bars across the universe; size, filter and persist ranked signals.

- Strategies are **discovered dynamically** from `stockscan/strategies/` at startup — see §4.11. The scanner iterates `STRATEGY_REGISTRY` and calls each strategy's contract methods; it never imports a strategy by name.
- Per run (`ScanRunner.run(strategy, as_of)`):
  1. Resolve the point-in-time S&P 500 universe for `as_of`.
  2. Read the day's market regime (§4.14): the trend gate, the vol scalar and the credit-stress flag. A missing regime row sizes neutrally and logs a warning.
  3. Build a `PortfolioContext` from the DB: equity (latest `equity_history` row, else `STOCKSCAN_STARTING_EQUITY`), open positions, sector map and current sector exposure, earnings within 5 days, 20-day dollar volume.
  4. For every symbol with enough history, call `strategy.signals(bars, as_of)`. New longs are rejected outright with `trend_gate_closed` or `credit_stress_long_block` while the regime blocks them; otherwise each signal is sized by `size_for_strategy` (§4.7).
  5. Run the filter chain over the survivors, best score first, so the strongest candidates claim contended slots. Passing candidates count against the caps for the rest of the pass.
  6. Persist a `strategy_runs` row plus one `signals` row per candidate — `status='new'` for passing, `'rejected'` with `rejected_reason` for everything else — so the UI shows both.
- Signals carry `(symbol, side, strategy, strategy_version, score, suggested_entry, suggested_stop | None, suggested_target, metadata)`. `metadata` holds the indicator values behind the signal; the signal-detail page renders it.
- `as_of` defaults to today; a backdated scan uses historical membership and historical bars only.

### 4.4 Backtester (`stockscan.backtest`)

**Design choice: event-driven, sharing strategy, sizing and regime code with the live engine.** A backtest measures the system that trades live, not a simplified cousin.

Loop, one trading day at a time (`BacktestEngine.run()`):

1. **Exits.** For each open position run `strategy.exit_rules()` on `bars[≤ today]`. Exits — stops included — are the strategy's decision; **the engine applies no stop of its own.** Triggered exits fill at tomorrow's open.
2. **Entries.** Look up today's row of the regime frame (§4.14). If new longs are blocked, skip entries for the day. Otherwise run `strategy.signals()` over the point-in-time universe, size each signal with `size_for_strategy` (the strategy's rule × the vol scalar where it opts in), sort by score, and run the same `FilterChain` the scanner uses — with `sectors`, `sector_exposure` and `avg_dollar_volume_20d` populated, so the sector and ADV caps bind. Survivors fill at tomorrow's open.
3. **Mark to market** end-of-day equity from today's close.

- The regime frame is computed once per run from SPY bars (800 calendar days of warmup) and the HY OAS series via `stockscan.regime.rules.regime_frame` — the function the live detector reads its last row from. No SPY bars → controls disabled for the run, logged once.
- Fills: next-day open, `FixedBpsSlippage` (default 5 bp) in the direction that hurts, commission default $0.
- `BacktestConfig` carries the portfolio caps (15 positions, 8% per position, 25% per sector, 5% of ADV, 15% drawdown breaker), matching the live defaults.
- Outputs: `backtest_runs`, `backtest_trades` (with entry stop, R-multiple, MAE/MFE and the entry metadata snapshot), `backtest_equity_curve`; metrics via `stockscan.metrics` (CAGR, Sharpe, Sortino, max drawdown and duration, win rate, profit factor, expectancy, exposure).
- CLI: `stockscan backtest run STRATEGY [--from/--to] [-s SYMBOL ...] [--capital] [--slippage-bps]`, `backtest list`, `backtest debug STRATEGY SYMBOL` (replays `signals()` per day and tabulates fired / score / every metadata key), `backtest export RUN_ID` (JSON: trades + entry metadata + equity + regime overlay), `backtest profile STRATEGY`.

#### 4.4.1 Performance shape

The backtest's working baseline on a 10-symbol × 1-year run is on the order of ten seconds; full S&P 500 × 4 years extrapolates to ~40 minutes. Three architectural facts about where the time goes — keep these in mind before any perf change:

**The DB is not the bottleneck.** Counter-intuitive but well-established by profiling. Three caches do the heavy lifting:

  - `engine._bars()` pulls each symbol's full bar history once per run and slices via O(log n) `searchsorted` on every subsequent call (the old `cached[cached.index.date <= as_of]` mask built a Python `date` object array on every hit — the original sink). One DB round-trip per symbol.
  - `relative_strength` has two run-scoped caches: `_SECTOR_MAP` (symbol → composite symbol, fetched once) and `_COMPOSITE_BARS` (one bar fetch per sector composite, ~11 total). Plus `_COMPOSITE_CLOSES_BY_DATE` pre-normalizes each composite's close series to tz-naive midnight once, so per-call `_by_date` work only happens on the stock side.
  - `_members_cache` keys point-in-time S&P 500 membership by date.

  For a 4-year × 500-symbol run that's ~1,500 DB queries total against ~500K (symbol, day) strategy evaluations. The cost is in compute, not I/O. If a profile ever shows `get_bars` or `psycopg` in the top of `cumtime`, a cache is stale or someone introduced a new DB-touching primitive — fix that, don't reach for more caching.

**The cost lives in the per-(symbol, day) indicator recompute.** Every `signals()` call recomputes its moving averages, RSI, realized vol or regression slope, and the sector-relative return reindex on a trailing tail of a few hundred bars. ~500 × 1,000 days = 500K calls × several pandas-heavy operations each. That's the floor.

**The pandas anti-pattern that catches you twice.** Two history-worthy fixes both took the same shape — writing into a pandas Series one cell at a time:

  - `_wilder_smoothing` in `indicators/ta.py` originally seeded with a simple mean and iterated `out.iloc[i] = prev + (v - prev) / period` in Python. Each `.iloc[i] = ...` goes through chained-assignment detection, block consolidation, and cache invalidation — ~1ms of pandas overhead per write, dwarfing the float arithmetic. Backtest profile: 55s cumulative, 2.3 million `__setitem__` calls. Fix: run the recurrence on a NumPy `ndarray`, wrap to Series at the end. Same math, same NaN propagation, 55× faster on the function and ~5× on the whole backtest.
  - `_composite_closes` in `indicators/relative_strength.py` sliced via `full.loc[full.index.date <= as_of, "close"]`, which builds a 6,000-element Python `date` object array every call. Fix: `searchsorted` on the datetime index, same pattern as the engine's `_bars()` slice.

  The lesson: **pandas is fast in C, slow in Python.** Anywhere a hot loop touches a Series one element at a time — `.iloc[i] = ...`, `.iat[i, j] = ...`, per-element `.index.date` extraction, `.apply(func)` over rows — is a candidate for moving to NumPy and wrapping back to Series at the end. Vectorized pandas (`.rolling().mean()`, `.ewm(...).mean()`) is fine. Per-cell pandas in a loop is not.

**The discipline: profile before you optimize.** The DB-caching hypothesis sounded right and was wrong — the actual bottleneck was Wilder smoothing, which nobody would have guessed from reading the code. Use `stockscan backtest profile STRATEGY` (cProfile around `BacktestEngine.run()`, code in `stockscan.backtest.profile`); it takes the same arguments as `backtest run` and dumps a sorted hotspot list. Sort by `cumtime` first ("where wall-clock goes"); sort by `tottime` to find leaf-level hotspots (per-call dict / float / Series construction). `python tools/profile_backtest.py` is a thin shim for the same command. Snapshot of the journey:

  | After | Total run | Top function | Cumulative speedup |
  | --- | --- | --- | --- |
  | (baseline) | 69.0s | `_wilder_smoothing` 55.7s | 1.0× |
  | Wilder fix | 13.7s | strategy scoring 11.8s | 5.0× |
  | RS date-handling fix | 11.7s | strategy scoring 9.9s | 5.9× |

  Past 5.9× the top of the profile becomes diffuse — `series.__init__`, `where`, `clip`, `__getitem__`, `_arith_method` — death by a thousand cuts. The next meaningful jump would require a per-symbol feature cache (precompute RSI/SMA series once per symbol when bars are first loaded; strategies look up at `today`'s index instead of recomputing the rolling window). That's the natural Phase-N optimization if S&P 500 × 4y becomes painful in the iterative loop.

### 4.5 Position Manager (`stockscan.positions`)

**Responsibilities:** Source of truth for what we hold, what's been ordered, what's pending exit, the cost basis of every open lot, and the round-trip "trade" that anchors notes and stats.

- `trades` table: **the round-trip anchor.** Opens when the first lot is acquired for a (symbol, strategy) in an empty state; closes when all related lots are sold. Tracks aggregate realized P&L, holding period, MAE/MFE. This is the unit that the journal (Story 5) and notes (Story 6) attach to.
- `tax_lots` table: **one row per buy.** Belongs to a trade (`trade_id`). Tracks per-share cost basis for tax accounting.
- `positions` view: aggregate per (symbol, strategy, account) rolled up from open lots — convenience for the dashboard.
- `orders` table: outbound orders with broker IDs and fills; sells reference one or more `lot_id`s.
- **Specific-lot exit flow:** when a strategy's `exit_rules` trigger a partial sell, the UI presents the open lots ranked by FIFO (default suggestion), HIFO (tax-minimizing alternative), and a custom-pick view. User confirms which lots to close. The selected lot IDs are passed to the broker on the order (E*TRADE supports specific-lot identification via the `lotMethod`/`lotIdentifier` fields).
- Reconciliation loop: every morning before open and every evening after close, fetch broker positions/orders and diff against local lot state. Discrepancies (manual trades, dividends, splits, partial fills) generate Discord alerts.
- Exit decision flow runs after each daily close:
  1. For each open position (aggregated from lots), run the owning strategy's `exit_rules`.
  2. If exit triggered → present lot-selection UI; on confirm, enqueue a `MARKET_ON_OPEN` sell order for next session with explicit lot IDs.
  3. Otherwise, update trailing stop if applicable.
- Time stops, hard stops, and target exits are all expressible in `exit_rules` and apply to the *aggregate position*; lot selection happens at execution time.

**Note on automation tradeoff:** Specific-lot tracking adds a manual confirmation step before every sell. For full automation later, we can add a "default lot policy" (FIFO/HIFO/strategy-aware) that auto-selects without a prompt — design supports this via a `lot_selection_policy` config per strategy.

### 4.6 Broker Abstraction (`stockscan.broker`)

```python
class Broker(ABC):
    def get_account(self) -> Account: ...
    def get_positions(self) -> list[BrokerPosition]: ...
    def get_orders(self, status: OrderStatus | None = None) -> list[BrokerOrder]: ...
    def place_order(self, order: OrderRequest) -> BrokerOrder: ...
    def cancel_order(self, broker_order_id: str) -> None: ...
    def get_quote(self, symbol: str) -> Quote: ...

# Implementations
class ETradeBroker(Broker):    ...  # OAuth 1.0a, pyetrade
class AlpacaBroker(Broker):    ...  # future, REST
class PaperBroker(Broker):     ...  # in-process sim, used by backtester
class SuggestionBroker(Broker):...  # never executes; logs/emails ideas
```

**Suggestion Mode mechanics:** `SuggestionBroker.place_order` does not transmit anything. Instead it persists the order to a `suggestions` table and renders it in the UI's "Today's Ideas" panel with a one-click "Mark as manually executed → log fill" button. This is the default broker for v1 and remains the fallback whenever broker auth is unavailable.

### 4.7 Risk Engine & Sizer (`stockscan.risk`)

**Sizer (`sizer.py`).** Two rules, chosen by whether the signal carries a stop; both cap notional at `max_position_pct` of equity and round down to integer shares:

- **Stop-based** — `qty = floor(equity × risk_pct / (entry − stop))`. Momentum: `default_risk_pct = 0.0075` against its 15% stop (≈5% of equity per position).
- **Fixed fraction** — `qty = floor(equity × position_pct / entry)`, `risk_dollars` = the full notional. RSI(2): `position_pct = 0.10`, no stop (stops cut this trade's returns more than its drawdown — Kaminski & Lo 2014; Alvarez).

`size_for_strategy(strategy_cls, equity, entry, stop, *, vol_scalar, max_position_pct)` applies the strategy's declared rule, then multiplies by the regime layer's vol scalar **only if** `strategy_cls.sizes_down_in_high_vol` is true (momentum yes, RSI(2) no — §4.14). It is the one sizing path; the live runner and the backtest engine both call it.

**Filter chain (`filters.py`).** Pure functions of `(signal, qty, PortfolioContext)`; first rejection wins, and the reason is persisted on the signal:

| Filter | Rejects when |
|---|---|
| drawdown circuit breaker | equity is more than `STOCKSCAN_DRAWDOWN_CIRCUIT_BREAKER` (15%) below its high-water mark |
| already in position | the symbol is held by any strategy |
| earnings within 5 trading days | gap risk on a small-edge trade |
| max positions | `STOCKSCAN_MAX_POSITIONS` (15) open positions portfolio-wide |
| per-strategy positions | the strategy's own `max_open_positions` (momentum: 10) |
| max position pct | notional > 8% of equity |
| max sector pct | sector exposure incl. this order > 25% of equity (unknown sector passes) |
| max ADV pct | notional > 5% of the symbol's 20-day average dollar volume (unknown ADV passes) |

Both the scanner and the engine populate `sectors`, `sector_exposure` and `avg_dollar_volume_20d` on the context, so the sector and ADV caps bind in both paths. Equity comes from the latest `equity_history` row, else `STOCKSCAN_STARTING_EQUITY` (default $100,000).

Regime entry blocks (trend gate closed, credit stress) are applied by the runner and the engine *before* sizing, not by the chain.

### 4.8 Web UI (`stockscan.web`)

**Stack:** FastAPI + Jinja2 + HTMX + Tailwind. No SPA. Charts via lightweight-charts.js (TradingView's open-source library — fast, designed for OHLC, native touch + pinch-zoom).

**Pages (v1):**
- **Dashboard** — equity curve, today's P&L, open positions, latest signals, system health.
- **Signals** — ranked candidates per strategy with a chart preview, rejection display, one-click "send to broker" / "mark suggestion taken".
- **Trades** — open + closed trades (round-trip view), per-trade P&L, strategy attribution, MAE/MFE.
- **Trade detail** — single-trade page with lots, sales, notes thread, base-rate-as-taken snapshot.
- **Base rates** — per-signal historical outcome analyzer page (Story 4).
- **Backtests** — list of saved runs, comparison view, equity curves, trade logs.
- **Strategies** — each strategy's manual, sizing rule and tuning knobs (read off the class), data-input freshness.
- **Analysis** — per-symbol trend bucket, realized-volatility state, options context, insider activity.
- **Regime card** (Dashboard) — trend gate with days on side, vol scalar with realized vol and rank, credit-stress flag, per-strategy sizing line.

**Mobile-first responsive (v1 requirement, USER_STORIES §Responsive):**

The same FastAPI + HTMX + Tailwind stack delivers both desktop and mobile from a single codebase — no separate mobile app. Tailwind's mobile-first breakpoint system (`sm:` `md:` `lg:`) drives the responsive scaling. Concrete rules:

- **Layout adapts at 640px (`sm:`).** Below that = phone layout; above = desktop.
- **Tables → stacked cards on phone.** Each row in scan results, trade lists, and signal lists becomes a vertical card with the most-important fields prominent. Implemented as HTMX-friendly partial templates that switch via Tailwind responsive classes — no JS forking.
- **Trade ticket is a full-screen route on mobile** (`/ticket/<signal_id>`), modal overlay on desktop. Same form, two layouts.
- **Sidebar nav → hamburger top bar on phone.**
- **Touch ergonomics:** all interactive elements ≥44px tall; no hover-dependent UI; numeric inputs use `inputmode="decimal"`.
- **Charts (`lightweight-charts`):** auto-fit to viewport width, native pinch-zoom and touch-pan, no dependency changes.
- **Markdown notes editor on mobile:** single textarea with a "Write / Preview" toggle (split view doesn't fit phone width).
- **No PWA, no offline mode, no push notifications in v1.** Discord and email cover push needs.

**Verification:** before Phase 2 sign-off, every primary workflow (scan → ticket → submit → view trade → add note → exit review → check base rates) is manually verified on a real iPhone (Safari) and a real Android (Chrome) via the WireGuard tunnel.

### 4.9 The refresh pipeline (`stockscan.jobs`)

`jobs.pipeline.run_pipeline` is the one fetch-and-analyze path in the app. It has two callers and no others: `stockscan jobs nightly-scan` (20:00 ET, Mon–Fri — supercronic via `infra/crontab` in the Compose stack, launchd plists on a bare Mac) runs it and sends the summary notification; the Dashboard's single **Refresh** button runs it on a background thread (`jobs.background`, single-flight: a click while a run is in flight joins that run, so double-clicks, second tabs and the MCP `refresh_data` tool all watch the same run) and shows each step as it completes in the strip at the top of the page, then reloads the Dashboard cards. There are no other refresh, fetch or rebuild buttons anywhere in the UI.

Steps, in order, each individually fault-tolerant (a failure is logged, recorded in `step_failures`, and the run continues):

1. **bars** — bulk-EOD for the sessions the store lacks up to the latest *completed* session (`data.backfill.missing_bulk_dates`, so a 2 pm click never fetches today's bar before it prints), filtered to the tracked set (`data.tracked.tracked_symbols`: S&P 500 ever-members + the watchlist), then `catch_up_lagging_symbols` fetches, per symbol, any watched name whose own latest bar is behind the freshest market-wide bar. The bulk pass judges freshness by the market, so without the catch-up a watched name that fell behind on its own would stay stale.
2. **macro** — FRED series (`BAMLH0A0HYM2` HY OAS for the credit-stress flag; `DGS1MO`/`DGS3MO` for the options analysis). Skipped with a warning when `FRED_API_KEY` is unset.
3. **regime** — `detect_regime(as_of, force_recompute=True)` from the fresh bars and macro, so a row cached earlier in the day is replaced before anything sizes against it.
4. **composites** — the equal-weight sector composites the strategies rank against, then every watchlist composite (local, no API calls).
5. **scans** — `ScanRunner.run()` for every registered strategy, **skipped** when no bar arrived and every strategy at its current version already has a run reaching the latest stored bar (`scan.store.has_run_covering`). A version bump or a new bar always rescans.
6. **trades** — paper trades marked to market and their exits applied.
7. **options** — tonight's short-premium book for all watched symbols (`proposals.store.save_run(replace=True)`: one saved run per date, an earlier run for the same day is replaced so the base rates never double-count a day), then `proposals.settle.settle_expired(as_of)` fills the outcome columns (`touched`, `breached`, `breach_date`, `close_at_expiry`, `max_adverse_pct`) of every proposal whose expiry has passed, from bars alone.
8. **feeds** — news, macro calendar, earnings and insider transactions, each only when the data plan includes it (`EODHD_FEATURES`) and its once-a-day cooldown (`refresh_log`, 20 h; insider's own 23 h gate) has passed.
9. **alerts** — watchlist price-target checks against the fresh bars.

The nightly run then sends the summary (email + Discord; the subject gains `DEGRADED` when any step failed). The pipeline is idempotent end to end: a second run with nothing new makes no bulk call, no catch-up call, no feed call, skips the scans and replaces today's regime row and options run with identical ones.

Daily DB backup (02:00 ET) and the weekly fundamentals refresh (Sun 03:00 ET) are separate cron lines. Broker order placement and reconciliation jobs arrive with Phase 4.

### 4.10 Notifications (`stockscan.notify`)

- Channels: **email** (Postmark or Gmail SMTP) + **Discord webhook**.
- Email is the primary channel for the nightly scan summary (rich HTML with chart thumbnails, ranked signals, P&L).
- Discord is for time-sensitive alerts: broker auth lapsed, reconciliation drift, exit fill, system error. Channel-based history makes it easy to scroll back and audit.
- Templates: nightly scan summary, exit triggered, order filled, reconciliation drift, broker auth required, system error.
- Implemented as pluggable `NotificationChannel` ABC so adding Pushover / ntfy / Slack later is a one-file change.

### 4.11 Strategy Plugin System (`stockscan.strategies`)

**Goal:** a strategy is one file that reads like a book. Drop it into `stockscan/strategies/`, restart, and the scanner, backtester, base-rate analyzer and UI pick it up. No registry edits, no framework changes.

#### Contract

Every strategy subclasses `Strategy` (`base.py`); subclassing registers it in `STRATEGY_REGISTRY` via `__init_subclass__`.

```python
class Strategy(ABC):
    # declarative metadata
    name: ClassVar[str]                     # "rsi2_meanrev"
    version: ClassVar[str]                  # "2.0.0" — bump on any logic or knob change
    display_name: ClassVar[str]
    description: ClassVar[str] = ""         # one paragraph (UI cards)
    manual: ClassVar[str] = ""              # long-form walkthrough, rendered on /strategies/<name>
    tags: ClassVar[tuple[str, ...]] = ()
    data_dependencies: ClassVar[tuple[str, ...]] = ()   # non-bar inputs, e.g. ("sector_composites",)

    # sizing (read by size_for_strategy and the filter chain)
    default_risk_pct: ClassVar[float] = 0.01        # stop-based sizing
    position_pct: ClassVar[float | None] = None     # fixed-fraction sizing (stop-less strategies)
    max_open_positions: ClassVar[int | None] = None
    sizes_down_in_high_vol: ClassVar[bool] = True   # does the regime vol scalar apply?

    def required_history(self) -> int: ...
    def signals(self, bars: pd.DataFrame, as_of: date) -> list[RawSignal]: ...   # pure; never reads past as_of
    def exit_rules(self, position: PositionSnapshot, bars, as_of) -> ExitDecision | None: ...

    @classmethod
    def knobs(cls) -> dict[str, int | float | str | bool]: ...   # every tunable constant on the class
    @classmethod
    def knobs_hash(cls) -> str: ...                             # identity of "which settings produced this run"
    @classmethod
    def code_fingerprint(cls) -> str: ...                       # SHA-256 of the source file
```

- **Knobs are class constants.** Every tunable is a `ClassVar` on the class; the strategy is instantiated with no arguments. `knobs()` collects every public int/float/str/bool attribute across the MRO (sizing attributes included, metadata excluded) for the strategy page, the run record and `stockscan strategies show`. To change a knob: edit the file, bump `version`. There is no parameter object, no DB shadow, no runtime override. Tests override per instance (`s = RSI2MeanReversion(); s.rsi_entry = 5.0`).
- **Exits are the strategy's alone.** `exit_rules()` returns an `ExitDecision(reason, qty)` or `None`; any stop — a price stop, a trend break, a time stop — is expressed there. Neither the runner nor the engine applies a stop of its own. A strategy that trades without a price stop emits `suggested_stop=None` and sets `position_pct`.
- **Regime is the runner's job.** Strategies declare `sizes_down_in_high_vol`; the runner and engine apply the trend gate, the vol scalar and the credit breaker (§4.14). Strategy code never reads the regime.
- `RawSignal` (frozen dataclass): `strategy_name, strategy_version, symbol, side, score, suggested_entry, suggested_stop | None, suggested_target, metadata`. `score` is the strategy's ranking metric; `metadata` holds the inputs behind it.

#### Auto-discovery

`discover_strategies()` imports every module in the package (skipping `_*` and `base`); subclassing does the registering. The scanner, backtester and analyzer consume `STRATEGY_REGISTRY` and never import a strategy by name.

#### Indicator primitives (`stockscan.indicators`)

Pure functions, Series in, Series (or float) out, NaN for insufficient history, no strategy argument and no registry. Strategies call them by name inside `signals()` / `exit_rules()` and keep the combining logic inline with comments in trader's language.

| Primitive | Module | Notes |
|---|---|---|
| `sma`, `ema`, `rsi`, `atr`, `true_range` | `ta.py` | Wilder smoothing runs on an ndarray (§4.4.1) |
| `avg_dollar_volume` | `ta.py` | 20-day default; feeds the ADV cap |
| `yang_zhang_volatility`, `yang_zhang_volatility_ewm` | `ta.py` | OHLC realized-vol estimators for the analysis page |
| `sector_return`, `sector_relative_return` | `relative_strength.py` | The one primitive that touches the DB: a symbol's sector composite (run-scoped caches for the sector map and composite bars) |

That is the whole set. Anything a strategy needs beyond it is computed inline in the strategy file.

#### Adding a strategy

```python
# stockscan/strategies/example_pullback.py
from typing import ClassVar

from stockscan.indicators import rsi, sma
from stockscan.strategies import ExitDecision, PositionSnapshot, RawSignal, Strategy

class ExamplePullback(Strategy):
    name = "example_pullback"
    version = "1.0.0"
    display_name = "Example Pullback"
    tags = ("mean_reversion", "long_only")

    position_pct = 0.05          # fixed fraction, no price stop
    sizes_down_in_high_vol = False

    rsi_period: ClassVar[int] = 2
    rsi_entry: ClassVar[float] = 10.0
    max_holding_bars: ClassVar[int] = 10

    def required_history(self) -> int: ...
    def signals(self, bars, as_of) -> list[RawSignal]: ...
    def exit_rules(self, position, bars, as_of) -> ExitDecision | None: ...
```

Drop the file, restart: the Strategies page shows the card with its knobs, the nightly job scans it, the backtester and base-rate analyzer can run it, and `strategy_versions` records its fingerprint on first use.

#### Testing contract

`tests/test_strategy_contract.py` parametrizes over every registered strategy: instantiates with no arguments; `required_history()` is positive and covers the longest lookback; a sizing basis is declared (`position_pct` or a positive `default_risk_pct`); `signals()` returns only `RawSignal`s carrying the strategy's own name and version, is idempotent, and is invariant to truncating `bars` at `as_of`; `exit_rules()` returns `None` or an `ExitDecision` and has no look-ahead; `knobs()` contains only primitives, includes the sizing attributes and excludes metadata; `knobs_hash()` is stable and sensitive to a knob change. A new strategy that violates the contract fails the suite before it is ever scanned.

#### Out of scope

Hot reload without restart; strategy upload or editing through the web UI (arbitrary code execution); strategies as separately installable packages; a parameter-sweep engine (settling backtests are run by editing knobs and bumping the version — `TODO.md`).

### 4.12 Base-Rate Analyzer (`stockscan.analyzer`)

**Responsibilities:** Given a signal `(strategy, symbol, as_of_date)`, compute historical outcome statistics for similar past setups on the same symbol — including setups that *would have been rejected by the current filters*. Backs USER_STORIES Story 4.

- For each historical date in the symbol's available history, run the strategy's `signals()` to identify when the same entry rule fired.
- For each historical setup, run the strategy's `exit_rules()` against forward bars to simulate the round-trip (consistent with the live and backtest engines — same code path).
- Replay the **filter chain** as it would have evaluated on that historical date; partition outcomes into "would have passed" vs "would have been rejected" cohorts.
- Compute per-cohort statistics: win rate, avg holding period, avg win/loss, profit factor, expectancy, max favorable/adverse excursion, return distribution.
- Layer **regime context** on top: split each cohort by index regime (SPY > 200 SMA bull vs bear) so the user sees regime-conditional edge.
- Emit a `BaseRateReport` dataclass that the web UI renders.

**Important property:** the filter-impact comparison (passing vs rejected expectancy) is the unique value here. It tells you whether your filters add edge or destroy it — closing the loop between the scanner's rejections and historical reality.

**Sample-size guardrails:** flag any cohort with n < 50 as "directional only" in the UI to prevent over-reading small-sample noise.

### 4.13 Watchlist (`stockscan.watchlist`)

**Responsibilities:** Manually-tracked symbols, with optional `(target_price, target_direction)` price-target alerts. Backs USER_STORIES Story 11.

- `watchlist_items` table (migration 0003): `symbol UNIQUE`, `target_price`, `target_direction CHECK ('above'|'below')`, `alert_enabled`, `last_alerted_at`, `last_triggered_price`, `note`, `created_at`. CHECK constraint enforces `(target_price IS NULL) = (target_direction IS NULL)` so the pair is always consistent.
- `store.py`: `add_to_watchlist`, `remove_from_watchlist`, `set_target`, `toggle_alert`, `mark_alerted`, `list_watchlist` (with last-bar enrichment via window functions), `watchlist_symbols` (cheap set lookup for the Dashboard's "is this symbol watched?" decoration).
- `alerts.py`: `check_and_fire_alerts()` finds items where `target_satisfied AND alert_enabled`, sends a high-priority notification, marks `last_alerted_at`, and **flips `alert_enabled` to FALSE** to prevent re-firing daily. The user re-arms via the UI checkbox.
- Integrated into the nightly job: after `_send_summary` runs the strategy scans, `check_and_fire_alerts()` runs against the freshly-refreshed bars. Failures are caught and logged; they don't block the rest of the job.
- Web UI: `/watchlist` (list + add/edit/delete forms, mobile cards), `+ Watch` HTMX in-place buttons on Dashboard signal and open-position rows, "✓ watching" pill rendered statically on Dashboard load for symbols already on the list.
- CLI: `stockscan watchlist list|add|remove|check-alerts`.

**Why auto-disable on fire:** the alternative — re-firing daily as long as the price stays past the target — generates noise and trains the operator to ignore alerts. One firing per crossing event matches retail-watchlist conventions (Robinhood, Fidelity) and is more useful in practice.

### 4.14 Market Regime (`stockscan.regime`)

Two controls and one breaker, deliberately separate, because the evidence backs each for a different job and with a different sign per strategy family. Full design note with citations: `market_regime_detection.md`.

| Control | Rule | Effect |
|---|---|---|
| **Trend gate** | SPY close vs SMA(200); flips only after `TREND_DWELL = 3` consecutive closes on the other side | Closed → **no new long entries**. Open positions run their own exits. |
| **Vol scalar** | 20-day realized vol of SPY log returns, percentile-ranked over 252 days; in the top tercile the scalar is `clip(0.16 / realized, 0.5, 1.0)`, else 1.0 | Multiplies position size for strategies with `sizes_down_in_high_vol = True` (momentum). Never scales up. |
| **Credit-stress flag** | HY OAS (`BAMLH0A0HYM2`) above the 85th percentile of its trailing 252 observations **and** higher than 5 observations ago | Blocks new longs while it fires. |

- `rules.py` is pure pandas: `trend_gate`, `realized_vol`, `vol_pct_rank`, `vol_scalar`, `credit_stress_flag`, and `regime_frame(spy_close, hy_oas) -> DataFrame` with every control per bar. Every computation is a trailing window or a forward state machine, so recomputing on a truncated series matches the live value at the truncation point (`tests/test_regime_rules.py` holds the property test).
- `detect.py` — `detect_regime(as_of, force_recompute=False)` pulls two years of SPY closes and the HY OAS series, evaluates `regime_frame`, persists the last row to `market_regime` (one row per day, `methodology_version = 3`) and returns a `MarketRegime`. No SPY bars → `None`, callers size neutrally. No HY OAS → credit flag off, logged.
- `store.py` — the row: `trend_gate_open`, `days_on_side`, `spy_close`, `spy_sma200`, `spy_sma200_slope_20d`, `realized_vol_20d`, `realized_vol_pct_rank`, `vol_scalar`, `hy_oas_level`, `hy_oas_pct_rank`, `credit_stress_flag`, and the display label `regime ∈ {risk_on, risk_off, credit_stress}` (credit stress dominates). `MarketRegime.block_new_longs` and `.vol_multiplier` are what the runner reads.
- **One rule set, two readers.** The live runner reads the last row via `detect_regime`; the backtest engine evaluates the whole frame once per run. A backtest therefore measures the gate, the scalar and the breaker exactly as they trade live.
- The nightly job recomputes the row (forced) after the bars and macro refresh and before any scan (§4.9). The dashboard card shows each control with its inputs and a per-strategy line saying whether the vol scalar applies.

### 4.15 Fundamentals Layer (`stockscan.fundamentals`)

**Responsibilities:** Latest-snapshot fundamentals data per symbol, refreshed from EODHD's `/fundamentals/{TICKER}`. Backs USER_STORIES Story 13 and supplies the sector map behind the sector composites.

- **`fundamentals_snapshot` table** (migration 0005): one row per `symbol UNIQUE`. **38 typed columns** for the fields strategies actually filter on at scan time (`market_cap`, `sector`, `industry`, `shares_outstanding`, `pe_ratio`, `forward_pe`, `eps_ttm`, `dividend_yield`, `beta`, `week_52_high`/`low`, `day_50_ma`/`day_200_ma`, ratios, ...). The full provider response stays in `raw_payload` JSONB for any future field that doesn't yet have an extracted column.
- **Indexes:** partial DESC index on `market_cap` (used by `market_cap_percentile` queries) and `sector`.
- **`store.py`:**
  - `_extract_columns(payload)` — parses EODHD's nested response shape. Missing fields silently become `None`; the strategy abstains rather than incorrectly passing/failing.
  - `upsert_fundamentals(symbol, payload)` — `ON CONFLICT (symbol) DO UPDATE`.
  - `market_cap_percentile(symbol)` — uses Postgres `PERCENT_RANK()` over the snapshot table; returns float in `[0, 100]` or `None` if the symbol has no row.
  - `list_by_market_cap(limit)` — ranked listings.
- **`refresh.py`** + CLI command `stockscan refresh fundamentals [SYMBOLS...] [--current-only]`. One API call per symbol; ~500 calls for the full S&P 500. Run weekly (most fields change quarterly with earnings).
- **DataProvider ABC extended** with `get_fundamentals(symbol)` (default returns None; EODHDProvider overrides).

**Point-in-time companion:** `fundamentals_history` (migration 0023) holds shares outstanding per reporting period, extracted from the stored `raw_payload`, so the cap-weighted composite builder uses `shares(t) × price(t)` rather than today's share count. The snapshot table itself is latest-only; no strategy filters on it at scan time.


### 4.16 Options proposals (`stockscan.proposals`)

A cross-sectional layer, not a `Strategy`: it reads every watched name's `SymbolAnalysis` and builds a short-premium book in the same shape as the signal canon — hard filters, then one rank key, no weighted blend. `engine.py` is the checklist: the trigger is today's move in units of the name's own daily vol (`DAY_TRIGGER_SIGMA = 1.0`), sector-residual for put-sales (only residual reversal survives in large caps — the `rsi2_meanrev` canon) and raw for call-sales (a green day into the strike is a level bet); the trend bucket and the regime layer qualify the side (red day in `strong_down` and green day in `strong_up` are skipped; a closed gate forces put-sales to counter-trend alignment; credit stress skips put-sales outright); then the filters — earnings inside the expiry when the date is known (`earnings_known` is carried as a flag otherwise), HV percentile ≥ 25, 20-day ADV ≥ $25M, price ≥ $10. `rank_key = |move_sigma| × trend_align`, ties by HV percentile; EMA confluences are shown as a fact and never ranked. `portfolio.py` sets the book multiplier (vol scalar × 0.5 under stress, so the halving lands on call-sales), sizes each row in contracts as `floor(equity × 0.005 × book_mult / (strike × 100 × 2 × σ_tenor))`, and caps two per sector (the runner's sector map) and two per hand-maintained cluster. `service.generate_book` is the one entry point for the page, the MCP tool, the CLI and the nightly job; it also lists the high-importance US macro events inside the expiry for the header (shown, never a multiplier). Nightly, the run is saved (`option_proposal_runs` / `option_proposals`, migration 0027) and `settle.py` fills `breached / touched / breach_date / close_at_expiry / max_adverse_pct / settled_at` for expired rows from bars alone; `store.trigger_base_rates` aggregates the settled rows per side × trend bucket × gate state, which is what the "Why this trade" card shows once a class has 30 settled proposals. Every threshold is a starting value — the base rates are what set them.
---

## 5. Data Provider Selection

Researched current pricing and capabilities; **EODHD** wins on a single feature competitors don't ship: historical S&P 500 constituents.

| Provider | Tier | Price | Pros | Cons |
|---|---|---|---|---|
| **EODHD** | All-In-One | **$99.99/mo** | EOD + intraday + **fundamentals (incl. earnings dates)** + **historical S&P 500 constituents** + 30+yr history | API quirks |
| EODHD | Fundamentals | $59.99/mo | Same as above minus intraday | Less intraday coverage if we want it later |
| Polygon.io | Stocks Starter | $29/mo | Modern API, great docs, real-time WebSocket | 15-min delay, **no constituent history**, no fundamentals |
| Tiingo | Power | ~$30/mo | Excellent EOD quality, fundamentals, news | Limited intraday, no constituent history |
| Alpaca | Free | $0 | Free, integrated with broker | IEX-only quotes, no constituent history, thin history |

**Decision: EODHD All-In-One at $99.99/mo for v1.** Sits at the top of your stated budget and removes the question of whether EOD bars are bundled with the Fundamentals tier (EODHD's plans are typically additive, so $59.99 Fundamentals + EOD might end up being two SKUs anyway). All-In-One bundles:

1. **Historical S&P 500 constituents** for survivorship-corrected backtests.
2. **Earnings dates** for the earnings filter on both strategies.
3. **EOD bars** for daily scanning and backtesting.
4. **Intraday bars** for future use (regime filters, intraday confirmation, eventual day-trading expansion) — not strictly needed for v1 but nice headroom.

**Confirm before purchase:** check EODHD's current SKU bundling at [eodhd.com/pricing](https://eodhd.com/pricing); if the $59.99 Fundamentals plan is confirmed to include EOD US data, downgrade to that and save $40/mo.

References: [EODHD pricing](https://eodhd.com/pricing), [EODHD historical constituents](https://eodhd.com/financial-apis-blog/sp-500-historical-constituents-data).

**Why historical constituents matter:** Without them, your S&P 500 backtest is run against today's index members — which means you're implicitly only trading companies that survived. This is **survivorship bias** and it inflates backtest CAGR by 1–3% annually. Almost every retail backtester I've seen has this bug. Fixing it is a meaningful edge in honest evaluation.

---

## 6. Strategy Specifications

The book is two strategies, both with decades of published out-of-sample evidence, both simple enough to read the code and verify it against the spec. Each file's module docstring and `manual` carry the trader-language walkthrough and the sources; this section is the summary.

### 6.1 `rsi2_meanrev` v2.0.0 — RSI(2) Pullback in Uptrend

**Source:** Connors & Alvarez (2008); Alvarez (2015–2024); Da, Liu & Schaumburg (2014) on industry-residual reversal; Medhat & Schmeling (2022) on turnover; Kaminski & Lo (2014) on stop-loss rules; Nagel (2012) on reversal and VIX.

| | Rule |
|---|---|
| Setup | `adj_close > SMA(200)` · sector 1-month return > −5% · 2-day selloff volume < 1.5× the prior 50-day mean |
| Entry | `RSI(2) < 10` at the close → buy at next open (`require_hook = False` by default) |
| Rank | idiosyncratic drop = sector 1-month return − stock 1-month return; most stock-specific drop first |
| Exit | `close > SMA(5)` or `RSI(2) > 50` → sell at next open; time stop after 10 bars |
| Stop | **none** — a price stop sells the extreme the strategy is built to buy |
| Sizing | `position_pct = 0.10`; `sizes_down_in_high_vol = False` (reversal pays best in high vol); portfolio caps only |

Signal metadata: `rsi_2`, `sma_200`, `stock_return_1m`, `sector_return_1m`, `idiosyncratic_drop`, `relative_volume`.

### 6.2 `momentum_52w_high` v2.0.0 — 52-Week-High Momentum

**Source:** George & Hwang (2004); Jeon & Byun (2023); Clenow, *Stocks on the Move* (2015); Gray & Vogel, *Quantitative Momentum*; Han, Zhou & Zhu on the 15% stop; Daniel & Moskowitz (2016) and Barroso & Santa-Clara (2015) on volatility scaling; Blitz, Huij & Martens (2011) on residual momentum.

| | Rule |
|---|---|
| Eligible | `adj_close > SMA(200)` and `SMA(50) > SMA(200)` · no single-day move beyond ±15% in the last 90 bars · 1-year realized vol ≤ 60% · close ≥ 90% of the 252-day high |
| Rank | closeness (close ÷ 252-day high) + Clenow slope quality (90-day log-price regression slope × R², squashed to 0–1) + residual tilt (12-month return minus the sector composite's, capped ±25%) |
| Review | new entries only on Wednesday's close, filled Thursday's open; exits run every day |
| Exit | close ≤ entry × 0.85 (`stop_loss`) · close < SMA(100) (`below_sma100`) · close < 85% of the 252-day high (`left_near_high_set`) |
| Sizing | `default_risk_pct = 0.0075` against the 15% stop (≈5% of equity per position); `max_open_positions = 10`; `sizes_down_in_high_vol = True` |

Signal metadata: `closeness_52w`, `slope_quality`, `residual_return_12m`, `residual_tilt`, `realized_vol_1y`, `sma_50`, `sma_200`.

### 6.3 Why these two together

They want opposite markets. RSI(2) earns in choppy uptrends and its profits rise with volatility; momentum earns in sustained trends and crashes in high-vol rebounds. The regime layer encodes that sign difference (vol scalar applies to momentum only) while the shared trend gate keeps both out of new entries when the index is below its 200-day. Neither strategy uses a discretionary indicator beyond moving averages, RSI, realized vol and a regression slope; the retired alternatives failed data-snooping-corrected tests on modern US data (see `market_regime_detection.md` and the 2026-09 canon review).

## 7. Brokerage Integration: E*TRADE

### 7.1 Status (verified 2026-04)

- API is operational under Morgan Stanley.
- OAuth 1.0a (HMAC-SHA1) is still the required auth flow — no migration to OAuth 2.0 announced.
- Production access requires application + approval; sandbox available immediately.
- Most maintained Python client: [`pyetrade`](https://github.com/jessecooper/pyetrade).

References: [E*TRADE Developer](https://developer.etrade.com/home), [pyetrade](https://pypi.org/project/pyetrade/).

### 7.2 Auth flow

E*TRADE issues a daily OAuth token that expires at midnight ET and must be re-authorized via browser. Plan:

1. First-run setup wizard in the web UI walks the user through OAuth handshake.
2. Tokens encrypted at rest (Fernet, key derived from a passphrase prompted at server start, kept in-memory only).
3. Re-auth required daily — expose a single "reconnect" button in the dashboard. Until reconnected, the system falls back to **Suggestion Mode** automatically (no orders sent, signals still surfaced).

### 7.3 Risks specific to E*TRADE

- Manual daily re-auth is a UX wart — design assumes you're checking the dashboard once a day anyway.
- API quirks (lot tracking, order types) are documented but require careful testing in sandbox.
- Long-tail risk: Morgan Stanley sunsets the API. Mitigation: the broker abstraction means a swap to Alpaca is days, not weeks.

---

## 8. Database Schema (PostgreSQL 16 + TimescaleDB)

**Notes:**
- `account_id` is plumbed through every transactional table so future multi-account support is purely additive.
- Bars live as a **TimescaleDB hypertable** in the same database — no separate Parquet store. Compression policy reduces older chunks to ~10% of original size while keeping them queryable.
- Continuous aggregates pre-compute weekly and monthly OHLCV rollups from daily bars; the scanner uses these for higher-timeframe filters (e.g., weekly trend) without recomputing each scan.
- Schema migrations managed via a **custom SQL runner** in `stockscan.db_migrate` (Alembic was removed; runner reads `migrations/NNNN_*.sql` files, splits on top-level semicolons, and runs each statement under AUTOCOMMIT — required because TimescaleDB continuous aggregates can't be created inside a transaction). Tracking lives in `_migrations` (version, name, applied_at, checksum).
- **26 migrations shipped** (`ls migrations/` is the full story; `make db-status` shows what is applied). The DDL below shows the schema as of 0001. Later tables are described in their module sections: `watchlist_items` (§4.13), `fundamentals_snapshot` / `fundamentals_history` (§4.15), `macro_series` and `market_regime` (§4.14, v3 columns in `0025_regime_v3.sql`), `paper_trades` (stop optional since 0026), sector composites, news, options proposals and hedge tables.

```sql
-- ============================================================
-- Extension setup (run once on database init)
-- ============================================================
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ============================================================
-- Account registry (v1 has exactly one row)
-- ============================================================
CREATE TABLE accounts (
    account_id     BIGSERIAL PRIMARY KEY,
    broker         TEXT NOT NULL,
    broker_account_id TEXT,
    label          TEXT,
    account_type   TEXT NOT NULL CHECK (account_type IN ('taxable','ira','roth','paper')),
    base_currency  TEXT NOT NULL DEFAULT 'USD',
    active         BOOLEAN NOT NULL DEFAULT TRUE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================
-- Reference data
-- ============================================================
CREATE TABLE universe_history (
    symbol       TEXT NOT NULL,
    joined_date  DATE NOT NULL,
    left_date    DATE,
    PRIMARY KEY (symbol, joined_date)
);

CREATE TABLE corporate_actions (
    symbol       TEXT NOT NULL,
    action_date  DATE NOT NULL,
    action_type  TEXT NOT NULL CHECK (action_type IN ('split','cash_div','stock_div','spinoff')),
    ratio        NUMERIC(20,10),  -- splits: e.g. 2.0 for 2-for-1
    amount       NUMERIC(20,6),   -- dividends: cash per share
    raw_payload  JSONB,
    PRIMARY KEY (symbol, action_date, action_type)
);

CREATE TABLE earnings_calendar (
    symbol       TEXT NOT NULL,
    report_date  DATE NOT NULL,
    time_of_day  TEXT CHECK (time_of_day IN ('bmo','amc','unknown')),
    estimate     NUMERIC(12,4),
    actual       NUMERIC(12,4),
    fetched_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (symbol, report_date)
);
CREATE INDEX idx_earnings_date ON earnings_calendar (report_date);

-- ============================================================
-- BARS: TimescaleDB hypertable
-- ============================================================
CREATE TABLE bars (
    symbol       TEXT        NOT NULL,
    bar_ts       TIMESTAMPTZ NOT NULL,    -- 16:00 America/New_York for daily
    interval     TEXT        NOT NULL DEFAULT '1d',  -- '1d','1h','5m', etc.
    open         NUMERIC(14,6) NOT NULL,
    high         NUMERIC(14,6) NOT NULL,
    low          NUMERIC(14,6) NOT NULL,
    close        NUMERIC(14,6) NOT NULL,
    adj_close    NUMERIC(14,6) NOT NULL,
    volume       BIGINT        NOT NULL,
    source       TEXT        NOT NULL DEFAULT 'eodhd',
    fetched_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (symbol, interval, bar_ts)
);

-- Convert to hypertable, partitioned by time
SELECT create_hypertable('bars', 'bar_ts', chunk_time_interval => INTERVAL '1 year');

-- Compress chunks older than 7 days; keep symbol grouping for range scans
ALTER TABLE bars SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'symbol,interval',
    timescaledb.compress_orderby   = 'bar_ts DESC'
);
SELECT add_compression_policy('bars', INTERVAL '7 days');

-- Continuous aggregate: weekly bars, refreshed nightly
CREATE MATERIALIZED VIEW bars_weekly
WITH (timescaledb.continuous) AS
SELECT
    symbol,
    time_bucket('1 week', bar_ts) AS week_start,
    first(open, bar_ts)   AS open,
    max(high)             AS high,
    min(low)              AS low,
    last(close, bar_ts)   AS close,
    last(adj_close, bar_ts) AS adj_close,
    sum(volume)           AS volume
FROM bars
WHERE interval = '1d'
GROUP BY symbol, week_start;

SELECT add_continuous_aggregate_policy('bars_weekly',
    start_offset => INTERVAL '8 weeks',
    end_offset   => INTERVAL '1 day',
    schedule_interval => INTERVAL '1 day');

-- Monthly aggregate (analogous; omitted for brevity in this doc)

-- Idempotent upsert helper used by the data layer (every fetch goes through this)
-- INSERT ... ON CONFLICT (symbol, interval, bar_ts) DO UPDATE SET ...

-- ============================================================
-- Strategy registry, versions, and live config (§4.11 plugin system)
-- ============================================================

-- One row per strategy *version* the framework has ever seen. Append-only.
-- Bumping a strategy's `version` in code creates a new row at startup.
CREATE TABLE strategy_versions (
    strategy_name      TEXT NOT NULL,
    strategy_version   TEXT NOT NULL,
    display_name       TEXT NOT NULL,
    description        TEXT,
    tags               TEXT[] NOT NULL DEFAULT '{}',
    params_json_schema JSONB NOT NULL,        -- Strategy.knobs() snapshot at first sighting
    code_fingerprint   TEXT NOT NULL,         -- SHA-256 of the strategy module file
    first_seen_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (strategy_name, strategy_version)
);

-- ============================================================
-- Strategy runs and signals
-- ============================================================
CREATE TABLE strategy_runs (
    run_id            BIGSERIAL PRIMARY KEY,
    strategy_name     TEXT NOT NULL,
    strategy_version  TEXT NOT NULL,
    run_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    as_of_date        DATE NOT NULL,
    universe_size     INTEGER NOT NULL,
    signals_emitted   INTEGER NOT NULL,
    rejected_count    INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (strategy_name, strategy_version)
        REFERENCES strategy_versions(strategy_name, strategy_version)
);

CREATE TABLE signals (
    signal_id        BIGSERIAL PRIMARY KEY,
    run_id           BIGINT REFERENCES strategy_runs(run_id),
    strategy_name    TEXT NOT NULL,           -- denormalized for fast filtering
    strategy_version TEXT NOT NULL,           -- pinned at signal time; immutable
    symbol           TEXT NOT NULL,
    side             TEXT NOT NULL CHECK (side IN ('long','short')),
    score            NUMERIC(10,6),
    as_of_date       DATE NOT NULL,
    suggested_entry  NUMERIC(14,6),
    suggested_stop   NUMERIC(14,6),
    suggested_target NUMERIC(14,6),
    suggested_qty    INTEGER,
    rejected_reason  TEXT,
    metadata         JSONB,
    status           TEXT NOT NULL CHECK (status IN ('new','ordered','rejected','expired')),
    FOREIGN KEY (strategy_name, strategy_version)
        REFERENCES strategy_versions(strategy_name, strategy_version)
);
CREATE INDEX idx_signals_status_date ON signals (status, as_of_date);

-- ============================================================
-- Orders, lots, sales
-- ============================================================
CREATE TABLE orders (
    order_id          BIGSERIAL PRIMARY KEY,
    account_id        BIGINT NOT NULL REFERENCES accounts(account_id),
    signal_id         BIGINT REFERENCES signals(signal_id),
    broker_order_id   TEXT,
    broker            TEXT NOT NULL,
    symbol            TEXT NOT NULL,
    side              TEXT NOT NULL CHECK (side IN ('buy','sell')),
    qty               INTEGER NOT NULL,
    order_type        TEXT NOT NULL,    -- 'market','limit','stop','market_on_open'
    limit_price       NUMERIC(14,6),
    stop_price        NUMERIC(14,6),
    status            TEXT NOT NULL,
    submitted_at      TIMESTAMPTZ,
    filled_at         TIMESTAMPTZ,
    avg_fill_price    NUMERIC(14,6),
    commission        NUMERIC(10,4) NOT NULL DEFAULT 0
);

-- Round-trip "trade" anchor: opens when the first lot is acquired for a (symbol, strategy)
-- in an empty state; closes when all related lots are fully sold. Anchors notes and stats.
CREATE TABLE trades (
    trade_id          BIGSERIAL PRIMARY KEY,
    account_id        BIGINT NOT NULL REFERENCES accounts(account_id),
    symbol            TEXT NOT NULL,
    strategy          TEXT NOT NULL,
    entry_signal_id   BIGINT REFERENCES signals(signal_id),
    opened_at         TIMESTAMPTZ NOT NULL,
    closed_at         TIMESTAMPTZ,
    status            TEXT NOT NULL CHECK (status IN ('open','closed')),
    realized_pnl      NUMERIC(14,4),       -- populated at close
    holding_days      INTEGER,             -- populated at close
    max_favorable_excursion NUMERIC(8,4),  -- as % of entry, tracked daily on open trades
    max_adverse_excursion   NUMERIC(8,4)
);
CREATE INDEX idx_trades_status ON trades (status, account_id);
CREATE INDEX idx_trades_strategy_closed ON trades (strategy, closed_at) WHERE status = 'closed';

CREATE TABLE tax_lots (
    lot_id          BIGSERIAL PRIMARY KEY,
    account_id      BIGINT NOT NULL REFERENCES accounts(account_id),
    trade_id        BIGINT NOT NULL REFERENCES trades(trade_id),
    symbol          TEXT NOT NULL,
    strategy        TEXT NOT NULL,
    qty_original    INTEGER NOT NULL,
    qty_remaining   INTEGER NOT NULL CHECK (qty_remaining >= 0),
    cost_basis      NUMERIC(14,6) NOT NULL,   -- per-share, commission-included
    acquired_at     TIMESTAMPTZ NOT NULL,
    source_order_id BIGINT REFERENCES orders(order_id),
    closed_at       TIMESTAMPTZ
);
CREATE INDEX idx_lots_open ON tax_lots (account_id, symbol) WHERE qty_remaining > 0;
CREATE INDEX idx_lots_trade ON tax_lots (trade_id);

CREATE TABLE lot_sales (
    sale_id              BIGSERIAL PRIMARY KEY,
    sell_order_id        BIGINT NOT NULL REFERENCES orders(order_id),
    lot_id               BIGINT NOT NULL REFERENCES tax_lots(lot_id),
    qty_sold             INTEGER NOT NULL,
    sale_price           NUMERIC(14,6) NOT NULL,
    sold_at              TIMESTAMPTZ NOT NULL,
    realized_pnl         NUMERIC(14,4) NOT NULL,
    holding_period_days  INTEGER NOT NULL
);

-- Aggregate position view
CREATE VIEW positions AS
SELECT account_id, symbol, strategy,
       SUM(qty_remaining) AS qty,
       SUM(qty_remaining * cost_basis) / NULLIF(SUM(qty_remaining), 0) AS avg_cost,
       MIN(acquired_at) AS first_acquired
FROM tax_lots
WHERE qty_remaining > 0
GROUP BY account_id, symbol, strategy;

-- ============================================================
-- NAV history and suggestion-mode log
-- ============================================================
CREATE TABLE equity_history (
    account_id        BIGINT NOT NULL REFERENCES accounts(account_id),
    as_of_date        DATE   NOT NULL,
    cash              NUMERIC(16,4) NOT NULL,
    positions_value   NUMERIC(16,4) NOT NULL,
    total_equity      NUMERIC(16,4) NOT NULL,
    high_water_mark   NUMERIC(16,4) NOT NULL,
    PRIMARY KEY (account_id, as_of_date)
);

CREATE TABLE suggestions (
    suggestion_id    BIGSERIAL PRIMARY KEY,
    account_id       BIGINT NOT NULL REFERENCES accounts(account_id),
    signal_id        BIGINT NOT NULL REFERENCES signals(signal_id),
    suggested_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    action           TEXT NOT NULL,
    qty              INTEGER NOT NULL,
    user_action      TEXT NOT NULL DEFAULT 'pending'
                     CHECK (user_action IN ('taken','skipped','pending')),
    user_action_at   TIMESTAMPTZ,
    journal_notes    TEXT
);

-- ============================================================
-- Trade notes (USER_STORIES Story 6) — anchored to the round-trip trade
-- ============================================================
CREATE TABLE trade_notes (
    note_id          BIGSERIAL PRIMARY KEY,
    trade_id         BIGINT NOT NULL REFERENCES trades(trade_id) ON DELETE CASCADE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    note_type        TEXT NOT NULL CHECK (note_type IN ('entry','mid','exit','free')),
    body             TEXT NOT NULL,        -- markdown
    template_fields  JSONB,                -- structured fields when using a template
    -- Generated full-text search column for efficient queries
    body_tsv         tsvector GENERATED ALWAYS AS (to_tsvector('english', body)) STORED
);
CREATE INDEX idx_notes_trade ON trade_notes (trade_id, created_at);
CREATE INDEX idx_notes_fts   ON trade_notes USING GIN (body_tsv);

-- Edit history (notes are append-only by default; edits captured for audit)
CREATE TABLE trade_note_revisions (
    revision_id      BIGSERIAL PRIMARY KEY,
    note_id          BIGINT NOT NULL REFERENCES trade_notes(note_id) ON DELETE CASCADE,
    body_before      TEXT NOT NULL,
    template_fields_before JSONB,
    edited_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

**Sizing & growth expectations:**

| Scope | Rows | Uncompressed | TimescaleDB compressed |
|---|---|---|---|
| S&P 500 × 16 yrs daily | ~2M | ~250 MB | ~25 MB |
| Russell 3000 × 30 yrs daily | ~22M | ~2.5 GB | ~250 MB |
| S&P 500 × 5 yrs 1-min intraday (eventual) | ~245M | ~30 GB | ~3 GB |

A Mac mini with even a 256 GB SSD comfortably holds decades of full-universe intraday data once compression kicks in.

---

## 9. Tech Stack & Dependencies

```
Python 3.12+
├── Web:         fastapi, uvicorn, jinja2, htmx (vendored), Tailwind (built, self-hosted)
├── Data:        pandas, numpy, httpx, tenacity
├── Storage:     psycopg 3.x, sqlalchemy 2.x, raw-SQL migrations (stockscan.db_migrate)
├── DB engine:   PostgreSQL 16 + TimescaleDB 2.x (community edition, Docker)
├── Charts (FE): lightweight-charts (TradingView, MIT)
├── Indicators:  hand-rolled in stockscan.indicators (no third-party TA library)
├── Broker:      pyetrade ([broker] extra, Phase 4)
├── Notify:      smtplib / Postmark, Discord webhook
├── Scheduler:   supercronic (Compose) or launchd (Mac mini)
├── Config:      pydantic-settings (.env)
├── MCP:         fastmcp ([mcp] extra; stockscan mcp serve)
├── Testing:     pytest, hypothesis; "integration" marker for tests that need Postgres
└── Tooling:     ruff, mypy, uv
```

**Repo layout:** see the annotated tree in `README.md` § Project layout.

## 10. Deployment (Home Server)

- **Target: Apple Silicon Mac mini.** Low power, silent, native launchd, all dependencies have arm64 wheels.

### 10.1 Database (Docker Compose)

PostgreSQL 16 + TimescaleDB runs in Docker on the Mac mini, with a persistent volume on the internal SSD (or an external SSD if you want to keep the OS disk lean). Docker on Apple Silicon is well-supported via Docker Desktop or OrbStack (lighter, recommended).

```yaml
# infra/docker-compose.yml
services:
  db:
    image: timescale/timescaledb:2.17.2-pg16
    container_name: stockscan-db
    restart: unless-stopped
    environment:
      POSTGRES_DB: stockscan
      POSTGRES_USER: stockscan
      POSTGRES_PASSWORD_FILE: /run/secrets/db_password
    volumes:
      - ./pgdata:/var/lib/postgresql/data
    ports:
      - "127.0.0.1:5432:5432"   # bind LAN-private; not exposed beyond host
    secrets:
      - db_password
    shm_size: 1gb
secrets:
  db_password:
    file: ./db_password.secret  # gitignored, 0600 perms
```

- Connection string: `postgresql+psycopg://stockscan@127.0.0.1:5432/stockscan` (password from secret).
- Tunables to set in `postgresql.conf`: `shared_buffers=2GB`, `work_mem=64MB`, `maintenance_work_mem=512MB`, `effective_cache_size=8GB`, plus TimescaleDB's `timescaledb.max_background_workers=8`.
- One-shot setup script `infra/setup_db.sh` runs `CREATE EXTENSION timescaledb`; `stockscan db migrate` applies the schema.

### 10.2 Application

- Python app installed via `pipx install -e .` against system Python 3.12 (or `uv` if you prefer).
- FastAPI behind a local Caddy reverse proxy for HTTPS on LAN. Use `mkcert` to generate a locally-trusted cert for `stockscan.local`.
- **Phone access via existing WireGuard VPN.** No additional networking required — the phone connects to the home LAN over WireGuard from anywhere and reaches `https://stockscan.local` (or the LAN IP) like any local device.
- **One-time mkcert root-CA install on each phone:** export the mkcert root CA from the Mac mini (`mkcert -CAROOT`), AirDrop / email it to the phone, install via Settings → General → VPN & Device Management (iOS) or Settings → Security → Install certificates (Android). Removes the HTTPS warning and is needed once per device. Documented in `infra/docs/mobile-setup.md`.
- Secrets (E*TRADE consumer key/secret, EODHD token, Postmark token, Discord webhook URL, **DB password**) encrypted at rest via Fernet; key derived from a passphrase prompted at server start, kept in-memory only.
- **launchd jobs** live in `~/Library/LaunchAgents/`:
  - `com.stockscan.refresh-and-scan.plist` — M–F 20:00 ET.
  - `com.stockscan.place-orders.plist` — M–F 09:25 ET.
  - `com.stockscan.reconcile.plist` — M–F 16:05 ET.
  - `com.stockscan.web.plist` — `KeepAlive` web server.
  - `com.stockscan.db-backup.plist` — daily `pg_dump` at 02:00 ET (see below).

### 10.3 Backups

Two layers, because the database is now the irreplaceable asset:

1. **Logical backup (`pg_dump --format=custom`) nightly** to `~/backups/stockscan-YYYYMMDD.dump`. Rotate to keep 14 dailies, 8 weeklies. Restore via `pg_restore`. Compressed dumps for S&P 500 × 16 yrs are <100 MB.
2. **Physical / volume backup**: Time Machine of the Docker volume directory provides point-in-time recovery via macOS snapshots. Optionally `pg_basebackup` to a second disk hourly for tighter RPO.

Both backup paths are redundant by design — the database represents real money's worth of historical data and execution records.

### 10.4 Optional Parquet Export

A nightly `stockscan export bars` job dumps `bars` to partitioned Parquet under `~/exports/bars/` for portability and use with external tools (Jupyter, DuckDB, R). This is a *consumer* of the database, not a parallel store — Postgres remains authoritative.

---

## 11. Roadmap

| Phase | Status | Scope |
|---|---|---|
| **0 — Foundations** | ✅ Done | Repo, Docker Compose for TimescaleDB, custom SQL migration runner, EODHD client + idempotent bar ingest, historical bulk backfill, S&P 500 universe (live + historical, Wikipedia fallback), FastAPI skeleton, CLI, `SuggestionBroker` + `PaperBroker`. |
| **1 — Strategies + Backtester** | ✅ Done | Strategy plugin system (ABC, auto-discovery, registry, class-constant knobs, contract tests), indicator primitives, RSI(2) pullback, 52-week-high momentum, event-driven backtester sharing sizing and regime code with the runner, metrics, CLI (`run` / `list` / `debug` / `export` / `profile`). |
| **2 — Web UI** | ✅ Done | Dashboard (with the one Refresh button), Signals (passing + rejected), Signal detail attribution, Trades (lots + journal), Backtests, Base-rate analyzer, Strategies (manual + knobs), Analysis, mobile-first responsive layouts, docs hub. |
| **3 — Live Scanner + Notifications** | ✅ Done | Bulk EOD endpoint, nightly job (bars → macro → regime → composites → scans → options book → alerts → summary), supercronic + launchd, email + Discord, DEGRADED summaries. |
| **Watchlist** | ✅ Done | `watchlist_items`, price-target alerts with auto-disable, "+ Watch" quick-adds, sector-composite chart. |
| **Fundamentals** | ✅ Done | `fundamentals_snapshot` (38 typed columns + raw JSONB), `fundamentals_history` (point-in-time shares), weekly refresh cron. |
| **Sector composites** | ✅ Done | Equal-weight sector indices rebuilt nightly; `sector_return` / `sector_relative_return` primitives; both strategies rank against them. |
| **Market regime** | ✅ Done | Trend gate with dwell, realized-vol scalar with per-strategy opt-in, HY OAS credit-stress breaker; `regime_frame` shared by runner and engine; migration 0025. |
| **News** | ✅ Done | EODHD `/news` for general feed + watchlist, on-demand article reader, CLI `refresh news`. |
| **Options + hedging** | ✅ Done | Weekly short-premium proposals, delta-hedge daemon + playground, MCP tools. |
| **Strategy canon review (2026-09)** | ✅ Done | Book reduced to `rsi2_meanrev` + `momentum_52w_high` (both v2.0.0); knobs as class constants; strategy-owned exits with no engine stop; shared `size_for_strategy`; sector/ADV caps binding in backtests; regime v3. |
| **Settling backtests** | Pending | Ablations listed in `TODO.md` — point-in-time S&P 500, 5 bp, 2010–2026, walk-forward. |
| **4 — E*TRADE Integration** | Pending | OAuth handshake UI, `ETradeBroker` against sandbox, integration tests, paper-money rehearsal. |
| **5 — Hardening** | Pending | Reconciliation loop, drift alerts, error handling, performance reporting, weekly journal export. |
| **Strategy optimizer** | Deferred | See `TODO.md §High-impact`. Bayesian search + walk-forward + held-out validation + deflated Sharpe + per-trial persistence. |

**Where the product stands:** the scanner runs nightly, the regime layer gates and sizes the two strategies, the summary reaches email/Discord, any signal expands to its full attribution chain, and execution is manual. E*TRADE auto-execution is the next enhancement; the settling backtests are the next research task.

## 12. Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Survivorship bias in backtests | Use EODHD historical S&P 500 constituents; restrict per-day universe to actual members on that date |
| Look-ahead bias | Strategies receive `bars[bars.index <= as_of]` only; assert in tests |
| Overfitting to backtest | Walk-forward analysis; reserve 2024–2026 as out-of-sample; small parameter set per strategy |
| Broker outage / auth expiry | Auto-fallback to Suggestion Mode; alert via Discord |
| Data provider outage | Cache is the source of truth for backtests; live scan uses last good cache + alerts on missing day |
| Data corruption (split not applied) | Daily reconciliation: re-fetch last 5 trading days, diff against DB, alert on mismatch. Splits trigger a full re-adjust transaction across affected symbol's history |
| Database loss / corruption | Nightly `pg_dump` (14 daily + 8 weekly retention) + Time Machine of Docker volume + optional hourly `pg_basebackup` to second disk |
| Schema drift / migration failure | Custom SQL migration runner with checksum-on-disk-vs-recorded drift detection (`make db-verify`); each migration tested manually + via integration tests against a fresh container before merge |
| Bug introduced into strategy | Backtester and live engine share strategy code; integration tests run a known-input → known-output regression on each PR |
| Manual mistakes during E*TRADE re-auth | UI requires explicit "I have re-authed" click before transmitting; otherwise Suggestion Mode |
| Personal risk-management drift | Hard-coded portfolio circuit breakers (max DD, max positions, max sector); cannot be disabled at runtime |

---

## 13. Resolved Decisions & Remaining Defaults

### Resolved

| Question | Resolution |
|---|---|
| Earnings filter | Skip both MR and TF entries within 5 trading days of next earnings report |
| Tax-lot accounting | Specific-lot tracking; user picks at exit time, FIFO suggested |
| Multiple accounts | Single account v1; `account_id` plumbed through schema for future expansion |
| Notifications | Email (Postmark) + Discord webhook |
| Starting capital | `STOCKSCAN_STARTING_EQUITY` (default $100,000) until the broker sync exists; integer shares only |
| Indicator library | Hand-rolled primitives in `stockscan.indicators` (sma, ema, rsi, atr, true_range, ADV, Yang-Zhang vol, sector returns) — nothing else |
| Server hardware | Apple Silicon Mac mini, launchd |

### Defaults I'm choosing unless you object

| Question | Default | Rationale |
|---|---|---|
| Backtest window | 2010-01-01 → today (default `--from` is 5 years before `--to`) | Covers 2010s bull, 2020 COVID crash, 2022 bear, 2023–25 recovery. Walk-forward with the last 2 years held out for the settling backtests |
| Suggestion-mode outputs | UI panel + email digest + CSV export per scan | CSV makes it trivial to journal in Excel or pipe to a Google Sheet later |
| Source code hosting | GitHub private repo | CI via GitHub Actions; secret management via repo-level encrypted secrets |
| Backtest commission model | $0 (matches E*TRADE for US equities) | Configurable for sensitivity testing |
| Backtest slippage model | 5 bps fixed at next-day open | Conservative for liquid S&P 500 names; sensitivity-test at 10 bps |
| First strategy to ship | RSI(2) mean-reversion | Faster signal-to-validation loop than TF (more trades per backtest year) |
| **Strategy hot reload** | **No — restart required to pick up new/edited strategies** | Simpler, safer (no stale-state bugs from `importlib.reload`). Mac mini restart of the FastAPI process is <5 seconds. Reconsider in v1.5 if iteration friction becomes painful. |
| **Strategy web upload** | **No — files on disk only, edited via your editor of choice** | Web upload would mean executing arbitrary user-uploaded Python on the server. Even single-user, that's an unnecessary attack surface (session hijack → RCE). Strategy code is committed to the repo and deployed via the normal app deploy. |
| **Strategy knobs** | **Class constants; edit and bump the version** | No parameter object, no DB row, no sweep engine. An ablation is a knob edit plus a backtest run; the run record stores `knobs_hash()` so results stay attributable. |
| **Strategy tags** | `('mean_reversion', 'trend_following', 'breakout', 'momentum', 'long_only', 'short_only', 'pairs')` as the initial vocabulary | Free-form strings are allowed; UI surfaces tags as filter chips on the Strategies page and in scan grouping. |

---

## 14. Appendix: References

- Larry Connors & Cesar Alvarez, *Short Term Trading Strategies That Work* (2008) — RSI(2) origin.
- George & Hwang (2004), "The 52-Week High and Momentum Investing", *Journal of Finance*.
- Andreas Clenow, *Stocks on the Move* (2015) — regression-slope ranking, SMA(100) exit.
- Daniel & Moskowitz (2016), "Momentum Crashes"; Barroso & Santa-Clara (2015), "Momentum Has Its Moments".
- Kaminski & Lo (2014), "When Do Stop-Loss Rules Stop Losses?"
- Marcos López de Prado, *Advances in Financial Machine Learning* (2018) — bias avoidance, walk-forward design.
- Regime-layer evidence (Faber 2007; Moreira & Muir 2017; Harvey et al. 2018; Nagel 2012; Gilchrist & Zakrajšek 2012; …): `market_regime_detection.md`.
- [EODHD documentation](https://eodhd.com/financial-apis/)
- [E*TRADE Developer](https://developer.etrade.com/home)
- [pyetrade](https://github.com/jessecooper/pyetrade)
- [TradingView lightweight-charts](https://github.com/tradingview/lightweight-charts)
- [HTMX](https://htmx.org)
