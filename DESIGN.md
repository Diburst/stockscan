# Personal Stock Trading App — Design Document

**Author:** Thomas
**Status:** Draft v0.8 — Phases 0–3 implemented, regime detection upgraded to a soft-sizing composite
**Date:** 2026-04-29

> **v0.8 changes** (regime detection v2 — composite + soft sizing; migration 0010):
> - **Composite regime score** replaces the v1 ADX/SMA-only label as the primary regime output. Four components in [0, 1] (1 = healthy/calm), weighted per the research synthesis: **vol 0.40 / trend 0.25 / breadth 0.20 / credit 0.15**. The discrete `trending_up / trending_down / choppy / transitioning` label is preserved for the dashboard banner and back-compat, but it no longer drives sizing.
> - **No look-ahead is the load-bearing invariant.** Every percentile, z-score, and slope uses *trailing* rolling windows (default 252 trading days). `tests/test_regime_composite.py` enforces this with truncation-invariance property tests on each windowed function plus an end-to-end pipeline test.
> - **VIX-aware vol score** via EODHD's `.INDX` exchange path (`get_bars("VIX", ..., exchange="INDX")`). VIX OHLC is stored in the existing `bars` hypertable. `vol_score = 1 - rolling_pct_rank(VIX, 252)`.
> - **HY OAS credit-stress signal** via FRED (series `BAMLH0A0HYM2`). New `FredProvider` mirrors `EODHDProvider`'s retry/transport pattern; new `macro_series` table holds level-only daily series. Two outputs: a smooth `credit_score = 1 - rolling_pct_rank(HY OAS, 252)` and a discrete `credit_stress_flag` (rank > 0.85 AND rising over 5 days) — the latter is wired as a tail-risk circuit breaker (0.5x size, block new long entries).
> - **Soft sizing replaces the hard regime gate.** `Strategy.applicable_regimes` is deprecated; strategies declare `regime_affinity: dict[label, weight in [0,1]]` instead. The scanner runner computes `effective_qty = round(base_qty * affinity * (0.5 + 0.5*composite) * stress_mult)` per signal. The runner's `_check_regime` skip-path is gone; `_resolve_regime_factor` returns a `RegimeFactor` dataclass instead. A back-compat shim in `Strategy.__init_subclass__` auto-derives `regime_affinity` from any legacy `applicable_regimes` declaration (in-set → 1.0, out → 0.0) and emits a `DeprecationWarning`. The three shipped strategies were migrated to declare `regime_affinity` directly.
> - **Schema**: migration 0010 adds `macro_series` + 13 new columns on `market_regime` (composite + 4 components + VIX/HY OAS levels and ranks/z + `credit_stress_flag` + `methodology_version`). All v2 score columns are NULLABLE so partial rows persist when a data source is degraded.
> - **Graceful degradation per source**: if VIX bars are missing, `vol_score` is NULL and the composite renormalizes weights over the remaining 3 components. Same for RSP (breadth) and HY OAS (credit). The composite is NULL only when every component is missing. Tests cover each missing-source path individually in `tests/test_regime_detect_v2.py`.
> - **Dashboard banner** redesigned: composite score with a coloured bar, four per-component bars (vol/trend/breadth/credit), underlying levels (VIX, HY OAS rank + z), credit-stress badge when fired, and a per-strategy effective-sizing table (`affinity × composite × stress = effective`). Mobile-responsive layout preserved (banner stacks vertically below the sm breakpoint).
> - **Cache discipline**: `detect_regime` returns cached v2 rows (`methodology_version >= 2`) as-is; v1 cached rows get re-detected on first call so the composite columns can backfill. `force_recompute=True` bypasses the cache for backtest replay.
> - **Skipped per the research doc** (deferred, *not* a v0.8 limitation): HMMs (Tier 2 in the research doc), BOCPD/PELT change-point detection, regime-switching GARCH, ML/deep-learning regime classifiers, multi-state HMMs, and trader-blog indicators (Aroon, Vortex, Choppiness, Fisher, MESA).

> **v0.7 changes** (post-implementation reality check, reflecting what shipped):
> - **Watchlist** (§4.13): manually-tracked symbols with optional `(target_price, target_direction)` alerts. Auto-disable on fire to prevent re-spam. Discord/email via the nightly hook. UI on `/watchlist` + Dashboard "+ Watch" quick-adds (HTMX in-place swap, no page reload).
> - **Technical Confirmation Score** (§4.14): per-signal score in `[-1, +1]` derived from RSI(14) + MACD(12,26,9), with strategy-tag-aware scoring (mean_reversion vs trend_following branches). Plugin pattern mirrors strategies — drop a file in `technical/indicators/`, restart, it's live. Persisted to `technical_scores`. Displayed on Signals + Watchlist. Backfillable for historical signals via `stockscan technical backfill` (no API calls — uses local bars).
> - **Fundamentals layer** (§4.15): per-symbol latest snapshot from EODHD `/fundamentals/{TICKER}`, 38 typed columns + raw_payload JSONB. Powers the new Largecap Rebound strategy's market-cap percentile filter. Refresh via `stockscan refresh fundamentals`.
> - **Largecap Rebound strategy** (new in §6.3): counter-trend long entries on top-quintile-by-market-cap S&P 500 names trading below SMA(200), confirmed by RSI + MACD turning bullish.
> - **Bulk EOD endpoint** (§4.1): `/eod-bulk-last-day/{exchange}` provider method — one API call per trading day for all symbols. Used by the nightly daily-refresh path; per-symbol path stays for initial backfills.
> - **Migration runner internals** (§4.x): SQL files run statement-by-statement under AUTOCOMMIT to handle TimescaleDB continuous aggregates (which can't be in a transaction). `_split_sql_statements` strips comments + splits on top-level `;`.
> - **Schema**: 5 migrations now (0001 initial, 0002 backtest, 0003 watchlist, 0004 technical_scores, 0005 fundamentals).

> **v0.6 changes:** added **§4.12 Strategy Plugin System** — strategies are auto-discovered Python modules dropped into `stockscan/strategies/`, registered via `__init_subclass__` on the `Strategy` ABC. Each strategy declares a Pydantic parameter schema; the UI auto-renders editors from it. Strategies are **versioned** — historical signals reference `(strategy_name, strategy_version, params_hash)` so changing a strategy's logic or default params never invalidates past results. Schema gains `strategy_configs` and `strategy_versions` tables. Reference implementations in §6 are now explicit examples of the contract. Adding a new strategy is a ~50-line file drop, no framework edits required.

> **v0.5 changes:** mobile/responsive UI elevated to a v1 requirement (was v2). Web UI is **mobile-first responsive** — must be functional on a ~390px viewport from the user's existing WireGuard-connected phone. Tables collapse to cards, modals become full-screen routes on mobile, sidebar collapses to hamburger, tap targets ≥44px. No PWA in v1. Phase 2 extended by ~0.5 weeks to absorb the responsive work and on-device verification (iOS Safari + Android Chrome). Deployment notes added for one-time `mkcert` root-CA install on the phone.

> **v0.4 changes (from USER_STORIES.md):** added **base-rate analyzer module** (now §4.12) for per-signal historical outcome analysis including filter-rejected past setups; added explicit **`trades` round-trip table** to anchor notes and journal entries; added **`trade_notes`** table with optional templated fields and Postgres full-text search; scanner UI spec'd to display **rejected signals with badged reasons**; scanner extended to support **backdated `as_of` parameter** for research scans.

> **v0.3 changes:** storage moved from SQLite + Parquet to **PostgreSQL 16 + TimescaleDB** (community edition). Bars are now a TimescaleDB hypertable inside the same database as transactional data, with native compression and continuous aggregates for weekly/monthly rollups. Single source of truth simplifies joins (bars × earnings × signals × positions in one query) and backups (`pg_dump`). Database runs in Docker on the Mac mini. Optional nightly Parquet export is kept for portability.

> **v0.2 changes:** locked target = Apple Silicon Mac mini; indicator lib = `pandas-ta`; capital = $1M with integer shares only; earnings filter on both strategies (5-day exclusion); EODHD upgraded to All-In-One ($99.99/mo) to bundle EOD + Fundamentals + intraday and remove SKU-bundling ambiguity; tax-lot accounting = specific-lot (manual selection at exit); single-account v1 with `account_id` plumbed through schema for future multi-account; notifications = email + Discord webhook; $1M-specific liquidity and concentration limits tightened.

---

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
| Strategies (v1) | One mean-reversion (RSI(2)), one trend-following (Donchian) — see §6 |
| Capital | $1,000,000 starting equity, integer shares only (no fractional) |
| Risk per trade | 1% of equity default ($10k), configurable per strategy |
| Earnings filter | Skip both MR and TF entries within 5 trading days of next reported earnings |
| Brokerage | E*TRADE first, behind a `Broker` abstraction; "Suggestion Mode" is a first-class no-broker output |
| Accounts | Single account v1; schema includes `account_id` everywhere for future multi-account |
| Tax lots | Specific-lot tracking; user selects lots at exit time; FIFO as default suggestion |
| Data provider | EODHD All-In-One ($99.99/mo) — bundles EOD + intraday + fundamentals + historical S&P 500 constituents (see §7) |
| Tech stack | Python 3.12+, FastAPI, HTMX, **PostgreSQL 16 + TimescaleDB** (Docker), SQLAlchemy 2 + Alembic, pandas/NumPy, `pandas-ta` |
| Storage philosophy | Single source of truth: Postgres holds everything (bars, transactional). TimescaleDB hypertable for bars with compression + continuous aggregates. Optional nightly Parquet export for portability. |
| Hosting | Apple Silicon Mac mini, launchd scheduler, web UI on LAN over HTTPS |
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
- All bars are **split- and dividend-adjusted** (total return basis); raw close is also stored in a separate column for audit.
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

**Responsibilities:** Apply each strategy's entry rules to today's bars across the universe; emit ranked signals.

- Strategies are **discovered dynamically** from `stockscan/strategies/` at startup — see §4.11 for the plugin system. The scanner does not import strategies directly; it iterates `STRATEGY_REGISTRY` and calls each strategy's contract methods.
- Each strategy implements the `Strategy` ABC defined in §4.11 (declared `name`, `version`, `params_model`, `signals()`, `exit_rules()`, `required_history()`, optional `tags`).
- Signals carry: `(symbol, side, strategy, strategy_version, score, suggested_stop, suggested_target, metadata)`.
- The scanner runs the **filter chain** (`stockscan.risk`) over each raw signal: passing signals get `status='new'`; failing signals get `status='rejected'` with `rejected_reason` populated. **Both are persisted** so the scanner UI can display passing and rejected with badged reasons (USER_STORIES Story 1).
- The scanner accepts an `as_of` parameter (default = today) so the same engine can be invoked for **backdated research scans** ("what would have triggered on 2024-03-15?"). Backdated scans use historical S&P 500 membership and historical bars only — no leakage.
- Scanner persists signals to the `signals` table and emits a notification.

### 4.4 Backtester (`stockscan.backtest`)

**Design choice: event-driven, sharing strategy code with the live engine.** This avoids the classic "backtest looked great, live diverged" gap.

- Iterates day by day over a historical date range.
- For each date, restricts the universe to **historical S&P 500 members on that date**.
- Calls `strategy.signals(...)` and `strategy.exit_rules(...)` exactly as the live scanner does.
- A `PaperBroker` simulates fills at next-day open with configurable slippage (default: 5 bps).
- Commission model: configurable; default `$0` for E*TRADE US equities.
- Outputs:
  - Equity curve persisted as a `backtest_equity_curve` table (one row per (run_id, date)).
  - Trade log persisted as a `backtest_trades` table; CSV export available on demand.
  - Metrics: CAGR, Sharpe, Sortino, max drawdown, max DD duration, win rate, avg win/loss, profit factor, expectancy, exposure %.
  - Optional: walk-forward analysis (rolling train/test windows).
- CLI: `stockscan backtest rsi2 --from 2010-01-01 --to 2024-12-31 --capital 100000`.

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

- **Default rule:** Risk 1% of current equity per trade ($10k at $1M).
- Position size = `floor((equity × risk_pct) / (entry_price − stop_price))`. Integer shares only — no fractional support on E*TRADE.
- Per-strategy override (TF defaults to 0.75% because of wider stops and more positions).
- **$1M-tuned portfolio constraints** (configurable):
  - Max concurrent positions: **15** (10 MR + 5 TF, roughly).
  - Max single position: **8% of equity** ($80k) — capping outsized winners.
  - Max gross exposure: **100%** of equity (no leverage).
  - Max sector concentration: **25%** of equity (tighter than the 30% rule of thumb because $1M makes single-sector blowups material).
  - **Liquidity floor:** position size ≤ **5%** of the symbol's 20-day average dollar volume. At $1M, even the 1% risk on a low-ADV name can be a meaningful share of daily volume — this prevents being the marginal bid.
  - **Circuit breaker:** no new entries if equity has drawn down >15% from high-water mark (tightened from 20% — at $1M, a 15% DD is $150k and warrants a manual review).
- All constraints checked at signal-generation time; rejected signals are logged with rejection reason and surfaced in the UI so you see *why* a setup didn't make the cut.

### 4.8 Web UI (`stockscan.web`)

**Stack:** FastAPI + Jinja2 + HTMX + Tailwind. No SPA. Charts via lightweight-charts.js (TradingView's open-source library — fast, designed for OHLC, native touch + pinch-zoom).

**Pages (v1):**
- **Dashboard** — equity curve, today's P&L, open positions, latest signals, system health.
- **Signals** — ranked candidates per strategy with a chart preview, rejection display, one-click "send to broker" / "mark suggestion taken".
- **Trades** — open + closed trades (round-trip view), per-trade P&L, strategy attribution, MAE/MFE.
- **Trade detail** — single-trade page with lots, sales, notes thread, base-rate-as-taken snapshot.
- **Base rates** — per-signal historical outcome analyzer page (Story 4).
- **Backtests** — list of saved runs, comparison view, equity curves, trade logs.
- **Strategies** — view/edit parameter YAML, trigger ad-hoc scans.
- **Settings** — broker config, risk caps, notification channels, universe overrides.

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

### 4.9 Scheduler (`stockscan.scheduler`)

- Use **launchd** (Mac mini target). Declarative, survives reboots, integrates with the system log.
- Three jobs:
  - `refresh-and-scan` (20:00 ET, daily M–F): pull bars, compute indicators, run scanners, run exit checks on open positions, send notification.
  - `place-orders` (09:25 ET, M–F): if broker is connected, transmit pending orders.
  - `reconcile` (16:05 ET, M–F): pull broker positions/orders, diff with local state, alert on drift.
- Each job is a CLI subcommand (`stockscan run refresh-and-scan`) so they're triggerable manually for testing.

### 4.10 Notifications (`stockscan.notify`)

- Channels: **email** (Postmark or Gmail SMTP) + **Discord webhook**.
- Email is the primary channel for the nightly scan summary (rich HTML with chart thumbnails, ranked signals, P&L).
- Discord is for time-sensitive alerts: broker auth lapsed, reconciliation drift, exit fill, system error. Channel-based history makes it easy to scroll back and audit.
- Templates: nightly scan summary, exit triggered, order filled, reconciliation drift, broker auth required, system error.
- Implemented as pluggable `NotificationChannel` ABC so adding Pushover / ntfy / Slack later is a one-file change.

### 4.11 Strategy Plugin System (`stockscan.strategies`)

**Goal:** adding a new strategy is a single-file drop into `stockscan/strategies/`. No registry edits, no framework changes, no UI changes — the scanner picks it up on the next restart, the UI auto-renders an editor for its parameters, the backtester and base-rate analyzer can immediately run it.

#### Contract

Every strategy is a subclass of `Strategy` (an ABC):

```python
# stockscan/strategies/base.py

from abc import ABC, abstractmethod
from datetime import date
from typing import ClassVar
import pandas as pd
from pydantic import BaseModel

class StrategyParams(BaseModel):
    """Subclass per strategy. Pydantic gives us validation + UI form rendering."""
    pass

class Strategy(ABC):
    # --- declarative metadata (class attributes) ---
    name: ClassVar[str]                  # unique, snake_case ('rsi2_meanrev')
    version: ClassVar[str]               # semver-ish ('1.0.0'); bump on logic change
    display_name: ClassVar[str]          # human-readable ('RSI(2) Mean-Reversion')
    description: ClassVar[str]           # one paragraph, shown in UI
    tags: ClassVar[tuple[str, ...]] = ()  # 'mean_reversion', 'long_only', etc.
    params_model: ClassVar[type[StrategyParams]]
    default_risk_pct: ClassVar[float] = 0.01  # may be overridden per strategy

    def __init__(self, params: StrategyParams):
        self.params = params

    @abstractmethod
    def required_history(self) -> int:
        """Bars needed before signals() can produce output (e.g., 200 for SMA200)."""

    @abstractmethod
    def signals(self, bars: pd.DataFrame, as_of: date) -> list[RawSignal]:
        """Pure function. bars indexed by date, as_of is the close to evaluate.
        MUST NOT use any data after as_of (look-ahead = bug)."""

    @abstractmethod
    def exit_rules(
        self, position: PositionSnapshot, bars: pd.DataFrame, as_of: date
    ) -> ExitDecision | None:
        """Returns an exit decision (sell with reason) or None to hold."""

    # --- auto-registration ---
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if not getattr(cls, '__abstract__', False):
            STRATEGY_REGISTRY.register(cls)
```

`__init_subclass__` is the magic: defining a non-abstract subclass of `Strategy` registers it as a side effect of class creation. No decorators required.

#### Auto-discovery

At process startup the framework calls `discover_strategies()`:

```python
# stockscan/strategies/__init__.py

import importlib, pkgutil
from pathlib import Path

STRATEGY_REGISTRY = StrategyRegistry()

def discover_strategies():
    """Import every .py file in this package. Subclassing Strategy auto-registers."""
    pkg_path = Path(__file__).parent
    for module_info in pkgutil.iter_modules([str(pkg_path)]):
        if module_info.name.startswith('_') or module_info.name == 'base':
            continue
        importlib.import_module(f'stockscan.strategies.{module_info.name}')
    return STRATEGY_REGISTRY
```

The scanner, backtester, and base-rate analyzer all consume from `STRATEGY_REGISTRY` — never import a strategy by name.

#### Parameter management

Each strategy declares its params as a Pydantic model. **Code defines the shape; the database holds the current values**:

- The `strategy_versions` table (§8) records every (strategy_name, version) the framework has seen, with the JSON Schema of its params model. New versions are detected and registered at startup.
- The `strategy_configs` table holds the *current* parameter values per strategy. Editing a config writes a new row with a new `params_hash`; old rows are kept for audit and reproducibility. The active config per strategy is the most recent.
- The web UI's "Strategies" page renders an editable form for each strategy's params from the Pydantic schema (Pydantic → JSON Schema → form fields, via a small renderer).
- Every signal, order, and trade record in the DB references `(strategy_name, strategy_version, params_hash)`. **Past signals are immutable** — changing today's params doesn't rewrite yesterday's signal history.

#### Adding a new strategy: walkthrough

To add a new strategy, e.g., a Bollinger Band squeeze breakout:

```python
# stockscan/strategies/bbsqueeze_breakout.py

from pydantic import Field
from .base import Strategy, StrategyParams, RawSignal, ExitDecision

class BBSqueezeBreakoutParams(StrategyParams):
    bb_period: int = Field(20, ge=10, le=50, description="Bollinger Band lookback")
    bb_stddev: float = Field(2.0, ge=1.0, le=3.0)
    squeeze_pct: float = Field(0.02, description="Band width threshold for squeeze")
    atr_stop_mult: float = Field(2.0, ge=1.0, le=4.0)

class BBSqueezeBreakout(Strategy):
    name = "bb_squeeze_breakout"
    version = "1.0.0"
    display_name = "Bollinger Band Squeeze Breakout"
    description = "Enters long on close above upper Bollinger Band after a squeeze period."
    tags = ("trend_following", "breakout", "long_only")
    params_model = BBSqueezeBreakoutParams
    default_risk_pct = 0.01

    def required_history(self) -> int:
        return max(self.params.bb_period, 20) + 50  # buffer for ATR

    def signals(self, bars, as_of):
        # ... computation using self.params.bb_period etc.
        return []

    def exit_rules(self, position, bars, as_of):
        # ... ATR trailing stop, time stop, etc.
        return None
```

That's it. Drop the file, restart the server, and:
- The "Strategies" page shows a new card with the description, tags, and an editable form for the four parameters.
- The daily scanner runs it.
- The backtester can backtest it.
- The base-rate analyzer can analyze any signal it produces.
- All historical scans before today are unaffected.

#### Testing contract

Every concrete strategy is auto-tested by a parameterized base test in `tests/strategies/test_contract.py`:

- `signals()` returns only `RawSignal` instances with the strategy's own name and version.
- `signals()` is **idempotent** (same inputs → same outputs).
- `signals()` has **no look-ahead** — slicing `bars` to `bars.index <= as_of` and re-running yields identical output.
- `exit_rules()` is monotonic given the same position state.
- The Pydantic params model has at least one valid default instance.
- `required_history()` returns a positive integer bounded by some sane max (say, 1000 bars).

CI runs the contract tests against every registered strategy. A new strategy that violates the contract fails CI before merge.

#### What this enables

- **Parameter sweeps**: the backtester accepts a parameter grid (`bb_period in [15,20,25] × bb_stddev in [1.5,2.0,2.5]`) and runs the cartesian product in parallel. Output is a heatmap of expectancy by param combination. (Defer the UI for sweeps to v1.5; the engine supports it from day 1.)
- **A/B comparisons**: backtest two versions of the same strategy side-by-side on identical bars and compare metrics.
- **Strategy retirement**: deactivate a strategy in `strategy_configs` (set `active=false`) without deleting its history. Past trades remain attributed; no new signals fire.
- **Strategy library growth without framework drag**: in three years, you can have 30 strategies in the folder and the framework code hasn't changed.

#### Out of scope for v1 (see §13)

- Hot reload of strategy modules without restart.
- Strategy upload/edit through the web UI (security: arbitrary code execution).
- Strategies as separately installable Python packages via entry points.

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

### 4.14 Technical Confirmation Score (`stockscan.technical`)

**Responsibilities:** Per-signal score in `[-1, +1]` answering "do the technicals confirm what this strategy is trying to do?" Backs USER_STORIES Story 12.

- **Plugin pattern, identical to strategies.** `TechnicalIndicator` ABC with auto-registration via `__init_subclass__`. Drop a file in `technical/indicators/`, restart, it's live. Initial indicators: RSI(14) and MACD(12, 26, 9).
- **Each indicator implements two methods:**
  - `values(bars, as_of) -> dict | None` — raw computed values (e.g., `{"value": 28.4}` for RSI). Returns None on insufficient history (composite skips abstaining indicators).
  - `score(values, strategy) -> float` — confirmation score in `[-1, +1]`, branching by `strategy.tags`. `strategy=None` triggers neutral / direction-agnostic mode (used by the Watchlist).
- **Tag-aware routing:** `mean_reversion` strategies want LOW RSI / negative-rising MACD = +confirming; `trend_following` and `breakout` want HIGH RSI / positive-rising MACD. Adding a strategy with existing tags requires zero indicator code changes; adding a new tag (e.g., `momentum_reversal`) requires one branch per indicator.
- **Composite** = equal-weight average across indicators that produced a value. Persisted in `technical_scores` (migration 0004) keyed `(symbol, as_of_date, strategy_name)`. Watchlist neutral-mode rows use `strategy_name = '_neutral'`.
- **Computed by `ScanRunner`** after persisting each signal — both passing AND rejected (rejected ones get a score for diagnostic value).
- **Backfillable** for past signals via `stockscan technical backfill` — uses local bars only, no API calls. `recompute --since DATE` overwrites for after-the-fact scoring-formula changes.
- **Display:** new "Tech" column on `/signals` (LEFT JOIN at query time) and `/watchlist` (computed on-render in neutral mode, ~50 ms for typical watchlist size). Colored signed bar: green positive, red negative, grey near-zero.

**Important nuance for new strategies:** strategies whose entry rules already require RSI + MACD bullish (like Largecap Rebound) will get tech scores that frequently agree with the entry decision — the score's marginal information is the *magnitude* of the readings rather than independent confirmation. Adding orthogonal primitives (volume confirmation, distance-from-200-SMA) is the long-term fix; not blocking on it.

### 4.15 Fundamentals Layer (`stockscan.fundamentals`)

**Responsibilities:** Latest-snapshot fundamentals data per symbol, refreshed from EODHD's `/fundamentals/{TICKER}`. Backs USER_STORIES Story 13 and powers the Largecap Rebound strategy's market-cap filter.

- **`fundamentals_snapshot` table** (migration 0005): one row per `symbol UNIQUE`. **38 typed columns** for the fields strategies actually filter on at scan time (`market_cap`, `sector`, `industry`, `shares_outstanding`, `pe_ratio`, `forward_pe`, `eps_ttm`, `dividend_yield`, `beta`, `week_52_high`/`low`, `day_50_ma`/`day_200_ma`, ratios, ...). The full provider response stays in `raw_payload` JSONB for any future field that doesn't yet have an extracted column.
- **Indexes:** partial DESC index on `market_cap` (used by `market_cap_percentile` queries) and `sector`.
- **`store.py`:**
  - `_extract_columns(payload)` — parses EODHD's nested response shape. Missing fields silently become `None`; the strategy abstains rather than incorrectly passing/failing.
  - `upsert_fundamentals(symbol, payload)` — `ON CONFLICT (symbol) DO UPDATE`.
  - `market_cap_percentile(symbol)` — uses Postgres `PERCENT_RANK()` over the snapshot table; returns float in `[0, 100]` or `None` if the symbol has no row.
  - `list_by_market_cap(limit)` — ranked listings.
- **`refresh.py`** + CLI command `stockscan refresh fundamentals [SYMBOLS...] [--current-only]`. One API call per symbol; ~500 calls for the full S&P 500. Run weekly (most fields change quarterly with earnings).
- **DataProvider ABC extended** with `get_fundamentals(symbol)` (default returns None; EODHDProvider overrides).

**Caveat documented in code:** the table holds the *latest* snapshot per symbol, not point-in-time history. For backtests of past dates this means we apply *today's* market-cap percentiles to historical bars — minor look-ahead bias on the universe filter only (prices stay clean). True historical fundamentals (per-quarter snapshots) is a Phase 5 enhancement.

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

## 6. Strategy Specifications (Proposed for Review)

Both strategies are documented in the literature and have decades of out-of-sample evidence. Both are also simple enough that you can read the code and verify it matches the spec.

**Note:** these are **reference implementations** of the `Strategy` contract defined in §4.11. Adding a third strategy later is a single-file drop into `stockscan/strategies/`; no framework code changes.

### 6.1 Mean Reversion: RSI(2) Pullback in Uptrend (Connors)

**Source:** Larry Connors & Cesar Alvarez, *Short Term Trading Strategies That Work* (2008).

**Setup filter:** `Close > SMA(200)` — only buy in long-term uptrends.

**Entry:** When `RSI(2) < 10` at today's close, enter long at tomorrow's open.

**Exit (whichever first):**
- `Close > SMA(5)` (mean has reverted) → sell at next open.
- Hard stop: `entry_price − 2.5 × ATR(14)` intraday.
- Time stop: 10 trading days.

**Position sizing:** 1% equity risked from entry to hard stop.

**Filters:**
- Skip if average dollar volume (20d) < $50M (liquidity floor raised for $1M capital).
- **Skip if name reports earnings within 5 trading days** (avoids gap risk on small-edge trades).
- Skip if intended position size > 5% of 20d ADV (per §4.7 liquidity rule).

**Expected behavior:** Many trades, short holds (avg ~3 days), high win rate (60–70%), small avg win/loss ratio. Edge comes from frequency. Earnings filter expected to drop ~10% of would-be signals.

### 6.2 Trend Following: Donchian Channel Breakout (Turtle-style)

**Source:** Richard Dennis's Turtle Traders rules, simplified.

**Entry:** When today's close is the highest close of the trailing **20 trading days**, enter long at tomorrow's open.

**Initial stop:** `entry_price − 2 × ATR(20)`.

**Trailing exit:** Chandelier stop — `max(close, last 22d high) − 3 × ATR(22)`. Updated daily.

**Exit confirmation:** Close < 10-day low triggers exit at next open (Turtle "S1" exit rule).

**Position sizing:** 0.75% equity risked (wider stops mean more positions; lower risk-per-trade keeps total portfolio risk reasonable).

**Filters:**
- Skip if avg dollar volume (20d) < $100M (TF holds longer; bigger names move less violently).
- Skip if ADX(14) < 18 at entry (no trend → don't chase a breakout).
- **Skip if name reports earnings within 5 trading days** (avoids gap risk on entry; existing TF positions ride through earnings as the trailing stop intends).
- Skip if intended position size > 5% of 20d ADV.

**Expected behavior:** Few trades, long holds (avg weeks–months), low win rate (35–45%), large avg win/loss ratio. Edge comes from letting winners run.

### 6.3 Counter-Trend: Largecap Rebound

**Setup filter (all required):**
- Symbol's market cap is at or above the 80th percentile of S&P 500 (top quintile by market cap).
- `Close < SMA(200)` — stock is in a long-term downtrend.

**Entry triggers (all required):**
- `RSI(14) ≥ rsi_threshold` (default 45) AND `RSI(14) > yesterday's RSI(14)` — momentum is bullish AND rising.
- MACD(12, 26, 9) `histogram > 0` AND `histogram > yesterday's histogram` — bullish AND accelerating.
- Buy at next-day open.

**Exits** (whichever first):
- `Close ≥ SMA(50)` — counter-trend rally hits trend resistance, take profit.
- `Close ≤ entry − 2.5 × ATR(14)` — hard stop.
- Time stop at 10 trading days.

**Position sizing:** 1% equity risked from entry to hard stop.

**Filters:** earnings within 5 trading days (consistent with the other strategies); ADV liquidity floor still applies through the shared filter chain.

**Tags:** `("mean_reversion", "long_only", "swing")`.

**Expected behavior:** few trades, quality bias. Most setups will be filtered out by the SMA(200) + market-cap + bullish-momentum combination — by design. When it does fire, the trade is buying *quality* on weakness, not chasing momentum.

**Source:** synthesis of common counter-trend / O'Neil-style "buy quality on weakness" + standard RSI/MACD bullish-confirmation gating.

### 6.4 Why these three together

The three strategies have **different regime dependencies** — running all three diversifies the strategy stack so any single market regime doesn't kill total P&L:

- **RSI(2)** profits in choppy uptrending markets (mean reversion within an uptrend).
- **Donchian** profits in sustained trending markets (regardless of direction; we're long-only here, so up-trends).
- **Largecap Rebound** profits when sold-off quality names recover (counter-trend in long-term downtrends).

When markets chop sideways, RSI(2) carries the load. When they trend, Donchian does. When a sector or quality bracket sells off and starts to recover, Largecap Rebound fires. The three rarely all profit at the same time, but they also rarely all lose at the same time.

---

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
- **5 migrations shipped:** 0001 initial (everything in this snapshot), 0002 backtest tables, 0003 watchlist (`watchlist_items`), 0004 technical_scores (`technical_scores`), 0005 fundamentals (`fundamentals_snapshot`). The DDL below shows the schema as of 0001; later migrations are described in their respective module sections (§4.13–§4.15).

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
    params_json_schema JSONB NOT NULL,        -- from Pydantic .model_json_schema()
    code_fingerprint   TEXT NOT NULL,         -- SHA-256 of the strategy module file
    first_seen_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (strategy_name, strategy_version)
);

-- Active configuration per strategy. Editing params writes a new row.
-- The "active" config is the most recent (active=true) row per strategy_name.
CREATE TABLE strategy_configs (
    config_id          BIGSERIAL PRIMARY KEY,
    strategy_name      TEXT NOT NULL,
    strategy_version   TEXT NOT NULL,
    params_json        JSONB NOT NULL,        -- validated against params_json_schema
    params_hash        TEXT NOT NULL,         -- SHA-256 of canonical params_json
    risk_pct_override  NUMERIC(5,4),          -- overrides default_risk_pct if set
    active             BOOLEAN NOT NULL DEFAULT TRUE,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by         TEXT,                   -- 'system' or username (future)
    note               TEXT,                   -- "tightened RSI threshold", etc.
    FOREIGN KEY (strategy_name, strategy_version)
        REFERENCES strategy_versions(strategy_name, strategy_version)
);
CREATE UNIQUE INDEX idx_active_config_per_strategy
    ON strategy_configs (strategy_name) WHERE active = TRUE;

-- ============================================================
-- Strategy runs and signals
-- ============================================================
CREATE TABLE strategy_runs (
    run_id            BIGSERIAL PRIMARY KEY,
    strategy_name     TEXT NOT NULL,
    strategy_version  TEXT NOT NULL,
    config_id         BIGINT NOT NULL REFERENCES strategy_configs(config_id),
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
    config_id        BIGINT NOT NULL REFERENCES strategy_configs(config_id),
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
├── Web:         fastapi, uvicorn, jinja2, htmx
├── Data:        pandas, numpy, pyarrow (export only), httpx
├── Storage:     psycopg[binary,pool] 3.x, sqlalchemy 2.x, alembic (migrations)
├── DB engine:   PostgreSQL 16 + TimescaleDB 2.x (community edition, Docker)
├── Charts (BE): matplotlib (for backtest report PDFs)
├── Charts (FE): lightweight-charts (TradingView, MIT)
├── Broker:      pyetrade
├── Indicators:  pandas-ta
├── Notify:      smtplib (stdlib) or postmarker, discord-webhook
├── Scheduler:   launchd plists (Mac mini target)
├── Config:      pydantic-settings, YAML
├── Testing:     pytest, pytest-cov, hypothesis, testcontainers (Postgres in CI)
└── Tooling:     ruff, mypy, pre-commit
```

**Repo layout:**
```
stock-scan/
├── pyproject.toml
├── DESIGN.md
├── stockscan/
│   ├── __init__.py
│   ├── cli.py                  # entry point: `stockscan ...`
│   ├── config.py
│   ├── data/
│   ├── universe/
│   ├── strategies/
│   ├── scan.py
│   ├── backtest/
│   ├── positions/
│   ├── broker/
│   ├── risk.py
│   ├── notify/
│   ├── scheduler/
│   └── web/
├── tests/
├── infra/
│   ├── docker-compose.yml      # TimescaleDB service + persistent volume
│   └── launchd/                # plist templates for Mac mini deployment
├── alembic/                    # database migrations
└── data/                       # gitignored; Postgres volume + optional Parquet exports
```

---

## 10. Deployment (Home Server)

- **Target: Apple Silicon Mac mini.** Low power, silent, native launchd, all dependencies have arm64 wheels (verified for `pandas-ta`, `pyarrow`, `pyetrade`, `psycopg`).

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
- One-shot setup script `infra/setup_db.sh` runs `CREATE EXTENSION timescaledb`, then `alembic upgrade head` to apply schema.

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
| **0 — Foundations** | ✅ Done | Repo, Docker Compose for TimescaleDB, **custom SQL migration runner** (replaced Alembic), EODHD client + idempotent bar ingest, historical bulk-backfill job, S&P 500 universe (live + historical), FastAPI skeleton, CLI scaffolding, `SuggestionBroker` + `PaperBroker`. |
| **1 — Strategies + Backtester** | ✅ Done | Strategy plugin system (ABC, auto-discovery, registry, params Pydantic models, contract tests), RSI(2), Donchian, indicator helpers (RSI, ATR, ADX, SMA/EMA, Donchian channel, Bollinger, **MACD**, ADV), event-driven backtester, metrics module, CLI runners. |
| **2 — Web UI** | ✅ Done | Dashboard, Signals page (with rejected-signal display), Trades page (lots + journal), Backtests page, Base-rate analyzer page, Trade notes with templated entry/exit prompts and FTS, Strategies page, mobile-first responsive layouts. |
| **3 — Live Scanner + Notifications** | ✅ Done | Bulk EOD endpoint, launchd plists for nightly-scan / web KeepAlive / db-backup, nightly job orchestration, email (SMTP/Postmark) + Discord webhook channels, channel router. |
| **Watchlist** | ✅ Done | `watchlist_items` (migration 0003), price-target alerts with auto-disable on fire, "+ Watch" HTMX in-place quick-adds, integrated into nightly job. |
| **Technical Confirmation Score** | ✅ Done | Plugin system mirroring strategies, RSI(14) + MACD(12,26,9) with tag-aware scoring, `technical_scores` table (migration 0004), persisted by ScanRunner, displayed on Signals + Watchlist, `stockscan technical backfill/recompute`. |
| **Fundamentals Layer** | ✅ Done | `fundamentals_snapshot` (migration 0005), 38 typed columns + raw JSONB, `market_cap_percentile` helper, `stockscan refresh fundamentals`. |
| **Largecap Rebound strategy** | ✅ Done | Counter-trend long entries on top-quintile-by-market-cap names below SMA(200) confirmed by RSI + MACD turning bullish (§6.3). |
| **4 — E*TRADE Integration** | Pending | OAuth handshake UI, `ETradeBroker` against sandbox, integration tests, paper-money rehearsal. |
| **5 — Hardening** | Pending | Reconciliation loop, drift alerts, error handling, performance reporting, weekly journal export, signal-detail tech-score breakdown view, true historical fundamentals (point-in-time per quarter). |

**Critical milestone reached:** at end of "Phase 3 + Watchlist + Tech Score + Fundamentals + Largecap Rebound", you can run the scanner nightly, get an email/Discord summary of ranked ideas augmented with technical confirmation scores, alert on price targets for watched names, and execute manually. That's a usable product end-to-end. E*TRADE auto-execution is the next enhancement.

---

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
| Starting capital | $1,000,000, integer shares only |
| Indicator library | `pandas-ta` |
| Server hardware | Apple Silicon Mac mini, launchd |

### Defaults I'm choosing unless you object

| Question | Default | Rationale |
|---|---|---|
| Initial backtest window | 2010-01-01 → 2026-04-01 (16 years) | Covers 2010s bull, 2020 COVID crash, 2022 bear, 2023–25 recovery, 2026 partial. Reserve last 2 years (2024–2026) as out-of-sample for walk-forward |
| Suggestion-mode outputs | UI panel + email digest + CSV export per scan | CSV makes it trivial to journal in Excel or pipe to a Google Sheet later |
| Source code hosting | GitHub private repo | CI via GitHub Actions; secret management via repo-level encrypted secrets |
| Backtest commission model | $0 (matches E*TRADE for US equities) | Configurable for sensitivity testing |
| Backtest slippage model | 5 bps fixed at next-day open | Conservative for liquid S&P 500 names; sensitivity-test at 10 bps |
| First strategy to ship | RSI(2) mean-reversion | Faster signal-to-validation loop than TF (more trades per backtest year) |
| **Strategy hot reload** | **No — restart required to pick up new/edited strategies** | Simpler, safer (no stale-state bugs from `importlib.reload`). Mac mini restart of the FastAPI process is <5 seconds. Reconsider in v1.5 if iteration friction becomes painful. |
| **Strategy web upload** | **No — files on disk only, edited via your editor of choice** | Web upload would mean executing arbitrary user-uploaded Python on the server. Even single-user, that's an unnecessary attack surface (session hijack → RCE). Strategy code is committed to the repo and deployed via the normal app deploy. |
| **Parameter sweeps** | **Engine supports them in v1; UI ships in v1.5** | Backtester accepts a parameter grid and runs the cartesian product in parallel. CLI-only access in v1 (`stockscan backtest rsi2 --sweep params/sweep.yaml`); web UI for sweep config + heatmap output deferred to v1.5. |
| **Strategy tags** | `('mean_reversion', 'trend_following', 'breakout', 'momentum', 'long_only', 'short_only', 'pairs')` as the initial vocabulary | Free-form strings are allowed; UI surfaces tags as filter chips on the Strategies page and in scan grouping. |

If any of those defaults look wrong, flag them — otherwise I'll bake them into Phase 0.

---

## 14. Appendix: References

- Larry Connors & Cesar Alvarez, *Short Term Trading Strategies That Work* (2008) — RSI(2) origin.
- Curtis Faith, *Way of the Turtle* (2007) — Donchian breakout / Turtle rules.
- Marcos López de Prado, *Advances in Financial Machine Learning* (2018) — bias avoidance, walk-forward design.
- [EODHD documentation](https://eodhd.com/financial-apis/)
- [E*TRADE Developer](https://developer.etrade.com/home)
- [pyetrade](https://github.com/jessecooper/pyetrade)
- [TradingView lightweight-charts](https://github.com/tradingview/lightweight-charts)
- [HTMX](https://htmx.org)
