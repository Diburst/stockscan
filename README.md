# Stockscan

Personal swing-trading scanner, backtester, and position manager.

> See [DESIGN.md](./DESIGN.md) for the full system design and [USER_STORIES.md](./USER_STORIES.md) for functional behavior.

## Status

**Phases 0–3 complete plus a substantial set of feature additions.** What works today:

- **Data layer**: TimescaleDB hypertable for bars, idempotent ingest, EODHD client (per-symbol + bulk EOD + news endpoints), FRED client for macro series, historical S&P 500 universe with survivorship-bias correction
- **Strategy plugin system**: drop a Python file in `strategies/`, restart, it's live
- **Four reference strategies**: RSI(2) Mean-Reversion, Donchian Trend (v1.1: multi-window + volume + vol-expansion + Turtle 1L + RS filter), Largecap Rebound (counter-trend with fundamentals filter), 52-Week-High Momentum (George-Hwang)
- **Market regime classifier (v2 composite)**: continuous vol/trend/breadth/credit composite weighted 40/25/20/15, HY OAS credit-stress circuit breaker, soft per-strategy sizing multiplier (replaces v1 hard regime gates)
- **Event-driven backtester** sharing strategy code with the live engine; metrics module
- **Meta-labeling layer (optional `[ml]` extra)**: XGBoost binary classifier per strategy, triple-barrier labels, scoring runs at scan time as advisory metadata (`meta_label_proba`); `stockscan ml train` / `stockscan ml status` CLI; per-strategy model status visible in the web UI
- **Web UI** (mobile-first responsive): Dashboard with regime breakdown + news card + strategy banner, Signals (with Fetch Latest + freshness chip), Signal detail (full attribution: outcome, score derivation, sizing breakdown, regime context, technical confirmation, meta-label probability, params used, raw metadata), Watchlist, Trades, Backtests, Base-Rate Analyzer, Strategies (with model-status panel)
- **In-app news reader**: Dashboard news card with per-article expand-on-click, on-demand re-fetch from EODHD (not persisted, no content-rights concerns)
- **Watchlist** with per-symbol price-target alerts (above/below), auto-disable after firing, "+ Watch" quick-add from Dashboard
- **Technical confirmation score** (per-strategy, signed [-1,+1]) computed from RSI(14) + MACD; displayed alongside strategy score on Signals + Watchlist
- **Fundamentals layer**: 38 typed columns + JSONB raw payload from EODHD, market-cap percentile helper
- **Notifications**: email (SMTP / Postmark) + Discord webhook, fired by the nightly job
- **Scheduler**: launchd plist templates for nightly-scan, web KeepAlive, daily DB backup
- **Migration runner**: 11 SQL migrations, custom runner (replaces Alembic, AUTOCOMMIT-aware)

Pending: **Phase 4** (E*TRADE OAuth + broker integration), **Phase 5** (reconciliation drift detection, journaling polish), and the **strategy-optimizer / vol-targeting overlay** items in `TODO.md`.

---

## Setup

If you've done this before, jump to [Quick reference](#quick-reference). Otherwise read top-down — the steps depend on each other.

### Prerequisites

- **macOS 12+ on Apple Silicon** (Linux works with minor adjustments)
- **Docker Desktop or [OrbStack](https://orbstack.dev/)** — OrbStack is recommended on Apple Silicon (lighter, faster, free for personal use). Either way, the daemon must be running before any `make db-*` command.
- **[uv](https://docs.astral.sh/uv/)** — `make install` will install it for you if missing
- An **[EODHD](https://eodhd.com/) API key** (All-In-One plan, ~$99.99/mo — see DESIGN §7 for why this provider)

### One-time setup

**1. Clone and enter the repo**

```bash
cd stock-scan
```

**2. Generate the database password**

The Docker container reads it from a file; your `.env` references the same value. Strip URL-special characters so you don't have to encode them:

```bash
echo "$(openssl rand -base64 32 | tr -d '+/=')" > infra/db_password.secret
chmod 600 infra/db_password.secret
```

**3. Create your `.env` from the template and edit it**

```bash
cp .env.example .env
```

Then open `.env` and set two things:
- `DATABASE_URL` — paste the password from `infra/db_password.secret` into the URL:
  ```
  DATABASE_URL=postgresql+psycopg://stockscan:<PASTE_PASSWORD_HERE>@127.0.0.1:5432/stockscan
  ```
- `EODHD_API_KEY` — your key from eodhd.com.

**4. Install Python dependencies**

```bash
make install
```

This installs `uv` if missing, then runs `uv sync --all-extras` to create `.venv/` and install everything into it.

**5. Bring up the database and apply the schema**

```bash
make db-up           # starts TimescaleDB container; ~10s on first boot
make db-init         # creates timescaledb extension + applies all SQL migrations
```

**6. Verify the install**

```bash
make test            # ~165 tests should pass
make db-status       # should show migrations 0001..0005 applied, no pending
```

---

## Running the CLI

The `stockscan` executable lives in `.venv/bin/`, which is **not on your shell's PATH** by default. Three ways to invoke it:

```bash
# Option A — through uv (no shell state, recommended)
uv run stockscan strategies list
uv run stockscan health

# Option B — activate the venv (once per shell session)
source .venv/bin/activate
stockscan strategies list             # works directly after activation

# Option C — bypass the entry point entirely
python -m stockscan strategies list
```

The `Makefile` uses option A under the hood, which is why `make db-migrate`, `make run-web`, etc. work without any activation.

If you want bare `stockscan` available globally, add this to `~/.zshrc` (adjust the path):

```bash
export PATH="$HOME/path/to/stock-scan/.venv/bin:$PATH"
```

—but `uv run stockscan ...` is more portable across machines and venv recreations.

---

## Running the web server

```bash
make run-web
```

Available pages:

| URL | What it shows |
|---|---|
| `/` | Dashboard — equity, latest signals (with "+ Watch" quick-add), open positions, **Market Regime** card with full v2 composite breakdown + dropdown explanations per component, **strategy banner** with regime affinity + soft-sizing multipliers, **news card** with per-article expand-on-click reader |
| `/signals` | Today's passing + rejected signals, filterable by strategy. **Header strip**: "Last scan: Xh ago · N today" + "Bars current through: YYYY-MM-DD [fresh/Nd behind]" + ⟳ Fetch Latest button (HTMX-swapped: backfills 7 days of bars + re-runs every strategy). **Strategy score** and **technical score** columns |
| `/signals/{id}` | Full signal attribution: Outcome (entry/stop/qty/risk-per-share/notional), Score derivation (humanized strategy metadata with one-line tooltips per indicator), Position sizing (`base × affinity × composite_mult × stress_mult` math), Market regime context (full v2 components + intermediate signals + percentile ranks), Technical confirmation breakdown, Meta-label probability with interpretation guide, Strategy parameters used at scan time, Run context, raw JSONB fallback |
| `/signals/{id}/base-rates` | Historical-setup outcome stats for that strategy on that symbol |
| `/news/{article_id}/content` | HTMX fragment endpoint — re-fetches the article body from EODHD on demand (not persisted) |
| `/watchlist` | Watched symbols with last close, % change, volume, **technical score**, price target editor, alert toggle |
| `/trades` | Open + closed trades; round-trip stats |
| `/trades/{id}` | Single-trade detail with notes thread (markdown + FTS) |
| `/backtests` | Saved backtest runs |
| `/backtests/{id}` | Run detail with equity curve + trade log |
| `/strategies` | Registered strategies with descriptions, beginner-friendly manuals, and a **meta-label model status chip** per card (trained/untrained, last fit, holdout AUC) |
| `/strategies/{name}` | Strategy detail with rendered manual + Pydantic param schema + full **meta-label model panel** (training rows, base rate, holdout AUC, threshold metrics, fit timestamp, re-train CLI snippet) |
| `/health` | JSON status (DB, TimescaleDB extension, registered strategies) |
| `/docs` | **Documentation hub** — index of all repo markdown docs (README, DESIGN, USER_STORIES, TODO, MIGRATION, regime-research) plus the auto-generated CLI reference. Renders markdown with TOC + anchor links; CLI reference walks the live Typer command tree |
| `/docs/cli` | Auto-generated CLI reference. Captures `--help` for every `stockscan` command/group/leaf via `typer.testing.CliRunner` — single source of truth, never drifts |
| `/docs/{slug}` | Renders one of the registered markdown files (slugs: `readme`, `design`, `user-stories`, `todo`, `migration`, `regime-research`) |
| `/api-docs` | FastAPI's auto-generated Swagger UI (relocated from `/docs`) |
| `/api-redoc` | FastAPI's ReDoc alternative |
| `/api-openapi.json` | OpenAPI JSON spec |

Mobile-first responsive throughout — tables collapse to cards below 640px, modals become full-screen routes (notably the trade ticket).

---

## What does `stockscan refresh` actually fetch?

### `stockscan refresh universe`

One EODHD API call to `/fundamentals/GSPC.INDX`. Pulls and persists into `universe_history`:

- **Current S&P 500 members** — ~500 symbols
- **Historical members back to ~2000** — every symbol ever in the index, with `joined_date` and `left_date`. Total ~1,200–1,500 unique symbols across history.

Run **weekly** — the index turns over slowly. Required before any `refresh bars` or backtest.

### `stockscan refresh bars`

Backfills daily OHLCV bars for symbols. Defaults:

- **Symbols:** all symbols ever in the S&P 500 (current + historical, ~1,200–1,500 names). Use `--current-only` to fetch just the current ~500.
- **Start date:** 2010-01-01 (override with `--start YYYY-MM-DD`)
- **End date:** today (override with `--end YYYY-MM-DD`)
- **Interval:** daily

Hits EODHD's `/eod/{TICKER}.US` once per symbol. Initial backfill numbers:

| Scope | API calls | Bars | Disk (uncompressed) | Disk (TimescaleDB compressed) | Time |
|---|---|---|---|---|---|
| `--current-only` (~500 syms) | ~500 | ~2 M | ~500 MB | ~50 MB | 5–15 min |
| Default (all ~1,500 syms) | ~1,500 | ~6 M | ~1.5 GB | ~150 MB | 15–45 min |

Subsequent runs are **incremental** — each symbol re-fetches `last_cached_date − 5 days` to today, so a daily refresh takes seconds.

**Why default to all historical members?** A backtest of, say, 2015 needs bars for companies that were S&P 500 members back then but have since been removed (acquired, bankrupted, demoted). Without those bars, the backtest silently drops trades on delisted losers and inflates returns — that's survivorship bias. Fetching all ever-members eliminates it.

**Per-bar fields stored** in the `bars` table:

| Column | Source | Notes |
|---|---|---|
| `symbol` | input | |
| `bar_ts` | EODHD `date` | converted to 16:00 ET → UTC |
| `interval` | hardcoded `'1d'` | hooks for intraday in v1.5 |
| `open` / `high` / `low` / `close` | EODHD raw | unadjusted |
| `adj_close` | EODHD `adjusted_close` | split + dividend adjusted (use this for analysis) |
| `volume` | EODHD raw | shares |
| `source` | `'eodhd'` | per-row provenance |
| `fetched_at` | `NOW()` | populated by Postgres |

Primary key is `(symbol, interval, bar_ts)` — repeated calls **upsert** (no duplicates).

---

## Quick reference

After setup, day-to-day:

```bash
# Database
make db-up                                       # start postgres
make db-down                                     # stop postgres
make db-migrate                                  # apply pending migrations
make db-status                                   # show applied + pending
make db-verify                                   # detect checksum drift
make db-reset                                    # DROP + recreate (DANGEROUS)

# Data refresh
uv run stockscan refresh universe                # ~1500 historical S&P 500 members
uv run stockscan refresh bars                    # ~6M bars (all members, 2010+)
uv run stockscan refresh bars --current-only     # ~2M bars (current 500 only)
uv run stockscan refresh bars AAPL MSFT          # specific symbols
uv run stockscan refresh bars VIX --exchange INDX  # cash indices
uv run stockscan refresh daily --days 5          # bulk-refresh recent N days
uv run stockscan refresh fundamentals --current-only  # ~500 EODHD fundamentals calls
uv run stockscan refresh macro                   # FRED HY OAS (default series for regime composite)
uv run stockscan refresh macro BAMLH0A0HYM2 BAMLC0A0CMEY  # multiple FRED series
uv run stockscan refresh news                    # EODHD news for general feed + watchlist

# Inspect
uv run stockscan health                          # DB + extension + strategies
uv run stockscan strategies list
uv run stockscan strategies show rsi2_meanrev
uv run stockscan strategies show momentum_52w_high
uv run stockscan technical list                  # registered technical indicators

# Scanning (live signals into DB)
uv run stockscan scan run rsi2_meanrev           # one strategy, today
uv run stockscan scan run --all                  # every registered strategy
uv run stockscan scan run rsi2_meanrev --as-of 2024-03-15   # backdated

# Signals backfill (replay scans — version-aware skip-query, so a version bump
# automatically re-scans older-version dates without --force)
uv run stockscan signals backfill donchian_trend                     # 1yr daily, resumable
uv run stockscan signals backfill all --start 2024-01-01             # all strategies, custom range
uv run stockscan signals backfill rsi2_meanrev --every 5             # weekly only
uv run stockscan signals backfill donchian_trend --force             # ignore skip set entirely

# Signals admin (delete prior-version data after a strategy upgrade)
uv run stockscan signals delete -s donchian_trend -v 1.0.0           # confirm interactively
uv run stockscan signals delete -s donchian_trend -v 1.0.0 --yes     # script-friendly
uv run stockscan signals delete -s rsi2_meanrev -v 1.0.0 \
    --start 2020-01-01 --end 2023-12-31                              # bounded date range

# Meta-labeling (requires `uv sync --extra ml`)
# Defaults filter to the CURRENT registered strategy version; pass
# --strategy-version to re-train on historical-version signals.
uv run stockscan ml train donchian_trend                             # fit + pickle to ./models/
uv run stockscan ml train rsi2_meanrev --min-rows 50                 # lower the floor for small-N
uv run stockscan ml train donchian_trend --strategy-version 1.0.0    # train on legacy v1.0 signals
uv run stockscan ml status                                           # list trained models w/ holdout AUC

# Watchlist
uv run stockscan watchlist list
uv run stockscan watchlist add AAPL --target 200 --direction above
uv run stockscan watchlist remove 3
uv run stockscan watchlist check-alerts          # fire any pending now

# Technical scores
uv run stockscan technical backfill              # fill in missing scores for past signals
uv run stockscan technical recompute --since 2024-01-01  # overwrite existing

# Backtesting
uv run stockscan backtest run rsi2_meanrev --from 2020-01-01 --capital 1000000
uv run stockscan backtest run donchian_trend --from 2020-01-01
uv run stockscan backtest list

# Scheduled jobs (run by launchd in production)
uv run stockscan jobs nightly-scan               # refresh + scan + watchlist alerts + notify

# Web + tests
make run-web                                     # FastAPI dev server on :8000
make test                                        # unit tests (~165)
make check                                       # lint + typecheck + test
```

---

## Project layout

```
stock-scan/
├── DESIGN.md                # System design (authoritative)
├── USER_STORIES.md          # Functional spec
├── README.md
├── pyproject.toml
├── Makefile
├── migrations/              # Plain SQL (custom runner; no Alembic)
│   ├── 0001_initial_schema.sql        # bars, accounts, signals, trades, lots, notes ...
│   ├── 0002_backtest_tables.sql       # backtest_runs / trades / equity_curve
│   ├── 0003_watchlist.sql             # watchlist_items + price-target alerts
│   ├── 0004_technical_scores.sql      # per-(symbol,date,strategy) tech scores
│   ├── 0005_fundamentals.sql          # latest fundamentals snapshot per symbol
│   ├── 0006_backtest_r_multiple.sql   # R-multiple column on backtest trades
│   ├── 0007_backtest_trade_context.sql  # context columns for backtest trades
│   ├── 0008_market_regime.sql         # legacy ADX+SMA regime classifier (v1)
│   ├── 0009_news.sql                  # news_articles + symbols + tags + alerts + feed_config
│   ├── 0010_regime_composite.sql      # macro_series + v2 composite regime columns
│   └── 0011_regime_intermediate_signals.sql  # SMA-200 slope, RSP/SPY ratio, breadth gap
├── infra/
│   ├── docker-compose.yml             # TimescaleDB
│   ├── setup_db.sh
│   ├── scripts/
│   │   └── db_backup.sh               # pg_dump rotation
│   ├── launchd/                       # plist templates: nightly-scan, web, db-backup
│   │   └── INSTALL.md
│   └── docs/
│       └── mobile-setup.md
├── src/stockscan/
│   ├── cli.py                         # `stockscan ...` (db / refresh / scan / backtest /
│   │                                    watchlist / technical / jobs / strategies / ml /
│   │                                    signals)
│   ├── config.py                      # Pydantic settings
│   ├── db.py                          # SQLAlchemy engine + healthcheck
│   ├── db_migrate.py                  # SQL migration runner (Alembic replacement)
│   ├── tables.py                      # SQLAlchemy Core table definitions
│   ├── metrics.py                     # CAGR, Sharpe, Sortino, max DD, profit factor
│   ├── data/                          # Provider clients (EODHD + FRED + stub), store, backfill, macro_store
│   ├── universe/                      # S&P 500 membership management
│   ├── fundamentals/                  # Snapshot store + EODHD refresh + market_cap_percentile
│   ├── indicators/                    # RSI, ATR, Donchian, ADX, Bollinger, MACD, ADV
│   ├── strategies/                    # Plugin system + RSI(2) + Donchian (v1.1) + Largecap Rebound + 52w-high
│   ├── regime/                        # v2 composite classifier (vol/trend/breadth/credit) + store + detect
│   ├── technical/                     # Per-signal tech-score (RSI/MACD plugins, scorer, store)
│   ├── analyzer/                      # Per-signal historical base-rate analysis
│   ├── scan/                          # ScanRunner + signals_freshness + refresh_signals (Fetch Latest)
│   ├── risk/                          # Sizer + filter chain (earnings, sector, ADV, drawdown)
│   ├── broker/                        # Broker ABC + Suggestion + Paper (E*TRADE in Phase 4)
│   ├── backtest/                      # Event-driven engine + slippage + persistence
│   ├── positions/                     # Trade lifecycle helpers
│   ├── notes/                         # Trade notes CRUD + FTS search
│   ├── news/                          # EODHD news refresh + on-demand article reader + store
│   ├── ml/                            # Meta-labeling: features + labels + train + predict + on-disk store
│   ├── watchlist/                     # Store + alerts + nightly hook
│   ├── notify/                        # Email (SMTP) + Discord webhook + router
│   ├── jobs/                          # Nightly orchestration: refresh → scan → notify
│   └── web/                           # FastAPI app, routes, Jinja templates (mobile-first)
├── models/                            # Pickled XGBoost meta-label artifacts (per strategy)
└── tests/                             # ~165 tests
```

---

## Troubleshooting

**`Connection refused` on port 5432.** The database isn't reachable. Walk through:

1. `docker ps` — is Docker itself running? If not, start Docker Desktop / OrbStack.
2. `docker ps | grep stockscan-db` — is the container up? If not, `make db-up`.
3. After `make db-up`, wait 5–10s and retry. First boot is slowest.
4. If the container shows `Up (unhealthy)`: `docker logs stockscan-db --tail 50`. Common causes: missing or empty `infra/db_password.secret`, port 5432 already taken on the host.
5. Confirm the password in `.env` `DATABASE_URL` matches `infra/db_password.secret`. Mismatched password = "password authentication failed" (different error) or it'll fall through to "connection refused" depending on the path.
6. Direct sanity check (bypasses `.env`):
   ```bash
   docker exec -it stockscan-db psql -U stockscan -d stockscan -c "SELECT 1;"
   ```

**`stockscan: command not found`.** The venv isn't on your PATH. Use `uv run stockscan ...` or `source .venv/bin/activate` first. See [Running the CLI](#running-the-cli).

**`make db-init` says "extension timescaledb already exists".** Safe to ignore — `setup_db.sh` uses `CREATE EXTENSION IF NOT EXISTS` and is idempotent.

**Web server returns 404 for `/`.** Earlier symptom — was true through Phase 0 only. The dashboard now lives at `/`. If you still see 404 on `/`, the FastAPI app may have failed to register the dashboard route — check the logs.

**`OperationalError: ... password authentication failed`.** The password in `.env` `DATABASE_URL` doesn't match what's in `infra/db_password.secret`. Update `.env`, then either restart the FastAPI server or just retry the CLI command — the connection pool reads the URL fresh each invocation.

---

## Phase status

| Phase | Status | What's in it |
|---|---|---|
| 0 — Foundations | ✅ Done | Repo, schema, data layer, plugin system, broker ABC, FastAPI skeleton, CLI |
| 1 — Strategies + backtester | ✅ Done | Indicators, RSI(2), Donchian, event-driven backtester, metrics, CLI |
| 2 — Web UI | ✅ Done | Dashboard, Signals, Trades, Backtests, Base-rates, Strategies — mobile-first responsive |
| 3 — Live scanner + notifications | ✅ Done | Bulk EOD endpoint, scheduler (launchd), nightly job, email + Discord, db-backup |
| Watchlist | ✅ Done | Per-symbol price-target alerts, auto-disable on fire, "+ Watch" quick-add, Discord/email alerts via nightly hook |
| Technical confirmation score | ✅ Done | Plugin system (RSI/MACD), per-strategy tag-aware scoring, signed [-1, +1], displayed on Signals + Watchlist |
| Fundamentals layer | ✅ Done | EODHD refresh, 38 typed columns + JSONB raw, market_cap_percentile helper |
| Largecap Rebound strategy | ✅ Done | Counter-trend long entries on top-quintile-by-market-cap S&P 500 names below SMA(200) |
| Market Regime v2 (composite) | ✅ Done | Vol (VIX) / Trend (SMA-200 slope) / Breadth (RSP-SPY) / Credit (HY OAS) composite (40/25/20/15). Soft per-strategy sizing multiplier replaces v1 hard gates. FRED provider, macro_series store, regime_affinity contract on Strategy. Dashboard component breakdown w/ per-component dropdown explanations |
| News integration | ✅ Done | EODHD `/news` for general feed + watchlist symbols, sentiment-aware ranking, dashboard card with **on-demand article reader** (per-row expand → re-fetch from provider, never persisted), CLI `refresh news` |
| 52-Week-High Momentum strategy | ✅ Done | George-Hwang style. Score = close / 252-day max, gated to within 5% of 52w high. Clenow regression-slope tiebreak. Time-based 60-day exit |
| Donchian v1.1 | ✅ Done | Multi-window ensemble (20+55), volume confirmation (1.5×), volatility-expansion (TR ≥ ATR(14)), Turtle 1L skip-after-winner filter (tracked as a rejected signal), relative-strength filter vs SPY (60d). Each filter individually toggleable for backtest A/B |
| Meta-labeling layer | ✅ Done | Optional `[ml]` extra. XGBoost binary classifier per strategy, triple-barrier labels (Lopez de Prado), 17 engineered features. CLI `ml train` / `ml status`, `signals backfill` populates training data, on-disk pickle store. Score-only integration: scan runner attaches `meta_label_proba` to signal metadata; never blocks trades |
| Signal-detail full attribution | ✅ Done | Outcome, Score derivation (humanized strategy metadata + tooltips), Position sizing math, Market regime context (every component + percentile rank + intermediate signal), Technical confirmation, Meta-label probability, Strategy params used at scan time, raw JSONB fallback |
| Signals freshness + Fetch Latest | ✅ Done | Header strip on `/signals` showing last scan + bars-current-through with [fresh/Nd behind] badge. POST `/signals/refresh` button: 7-day bulk-EOD bars catch-up + re-runs every registered strategy via HTMX |
| Strategy model status UI | ✅ Done | Strategies list shows per-card chip (no model / trained N days ago / AUC X.XX); detail page shows full meta-label panel with re-train CLI snippet |
| 4 — E*TRADE integration | Pending | OAuth flow, broker impl, fill reconciliation |
| 5 — Hardening | Pending | Reconciliation drift alerts, error handling, journal export |
| Strategy optimizer (Bayesian) | Pending | See [TODO.md §High-impact](TODO.md). Walk-forward + held-out validation + deflated Sharpe + per-trial persistence |
| Vol-targeting overlay (Moreira-Muir) | Pending | See [TODO.md §Medium-impact](TODO.md). Per-strategy realized-vol scaling for sizing |
