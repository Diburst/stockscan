# Stockscan

Personal swing-trading scanner, backtester, and position manager.

> See [DESIGN.md](./DESIGN.md) for the full system design and [USER_STORIES.md](./USER_STORIES.md) for functional behavior.

## Status

**Phases 0–3 complete plus several feature additions.** What works today:

- **Data layer**: TimescaleDB hypertable for bars, idempotent ingest, EODHD client (per-symbol + bulk EOD endpoints), historical S&P 500 universe with survivorship-bias correction
- **Strategy plugin system**: drop a Python file in `strategies/`, restart, it's live
- **Three reference strategies**: RSI(2) Mean-Reversion, Donchian Trend, Largecap Rebound (counter-trend with fundamentals filter)
- **Event-driven backtester** sharing strategy code with the live engine; metrics module
- **Web UI** (mobile-first responsive): Dashboard, Signals, Watchlist, Trades, Backtests, Strategies, Base-Rate Analyzer
- **Watchlist** with per-symbol price-target alerts (above/below), auto-disable after firing, "+ Watch" quick-add from Dashboard
- **Technical confirmation score** (per-strategy, signed [-1,+1]) computed from RSI(14) + MACD; displayed alongside strategy score on Signals + Watchlist
- **Fundamentals layer**: 38 typed columns + JSONB raw payload from EODHD, market-cap percentile helper
- **Notifications**: email (SMTP / Postmark) + Discord webhook, fired by the nightly job
- **Scheduler**: launchd plist templates for nightly-scan, web KeepAlive, daily DB backup
- **Migration runner**: 5 SQL migrations, custom runner (replaces Alembic, AUTOCOMMIT-aware)

Pending: **Phase 4** (E*TRADE OAuth + broker integration) and **Phase 5** (reconciliation drift detection, journaling polish).

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
| `/` | Dashboard — equity, latest signals (with "+ Watch" quick-add), open positions, system health |
| `/signals` | Today's passing + rejected signals, filterable by strategy, with **strategy score** and **technical score** columns |
| `/signals/{id}` | Per-signal detail: indicator values, suggested entry/stop, link to base-rates |
| `/signals/{id}/base-rates` | Historical-setup outcome stats for that strategy on that symbol |
| `/watchlist` | Watched symbols with last close, % change, volume, **technical score**, price target editor, alert toggle |
| `/trades` | Open + closed trades; round-trip stats |
| `/trades/{id}` | Single-trade detail with notes thread (markdown + FTS) |
| `/backtests` | Saved backtest runs |
| `/backtests/{id}` | Run detail with equity curve + trade log |
| `/strategies` | Registered strategies with descriptions and full beginner-friendly manuals |
| `/strategies/{name}` | Strategy detail with rendered manual + Pydantic param schema |
| `/health` | JSON status (DB, TimescaleDB extension, registered strategies) |
| `/docs` | FastAPI's auto-generated Swagger UI |

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
uv run stockscan refresh daily --days 5          # bulk-refresh recent N days
uv run stockscan refresh fundamentals --current-only  # ~500 EODHD fundamentals calls

# Inspect
uv run stockscan health                          # DB + extension + strategies
uv run stockscan strategies list
uv run stockscan strategies show rsi2_meanrev
uv run stockscan technical list                  # registered technical indicators

# Scanning (live signals into DB)
uv run stockscan scan run rsi2_meanrev           # one strategy, today
uv run stockscan scan run --all                  # every registered strategy
uv run stockscan scan run rsi2_meanrev --as-of 2024-03-15   # backdated

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
│   └── 0005_fundamentals.sql          # latest fundamentals snapshot per symbol
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
│   │                                    watchlist / technical / jobs / strategies)
│   ├── config.py                      # Pydantic settings
│   ├── db.py                          # SQLAlchemy engine + healthcheck
│   ├── db_migrate.py                  # SQL migration runner (Alembic replacement)
│   ├── tables.py                      # SQLAlchemy Core table definitions
│   ├── metrics.py                     # CAGR, Sharpe, Sortino, max DD, profit factor
│   ├── data/                          # Provider clients (EODHD + stub), store, backfill
│   ├── universe/                      # S&P 500 membership management
│   ├── fundamentals/                  # Snapshot store + EODHD refresh + market_cap_percentile
│   ├── indicators/                    # RSI, ATR, Donchian, ADX, Bollinger, MACD, ADV
│   ├── strategies/                    # Plugin system + RSI(2) + Donchian + Largecap Rebound
│   ├── technical/                     # Per-signal tech-score (RSI/MACD plugins, scorer, store)
│   ├── analyzer/                      # Per-signal historical base-rate analysis
│   ├── scan/                          # ScanRunner — strategies → filters → persisted signals
│   ├── risk/                          # Sizer + filter chain (earnings, sector, ADV, drawdown)
│   ├── broker/                        # Broker ABC + Suggestion + Paper (E*TRADE in Phase 4)
│   ├── backtest/                      # Event-driven engine + slippage + persistence
│   ├── positions/                     # Trade lifecycle helpers
│   ├── notes/                         # Trade notes CRUD + FTS search
│   ├── watchlist/                     # Store + alerts + nightly hook
│   ├── notify/                        # Email (SMTP) + Discord webhook + router
│   ├── jobs/                          # Nightly orchestration: refresh → scan → notify
│   └── web/                           # FastAPI app, routes, Jinja templates (mobile-first)
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
| 4 — E*TRADE integration | Pending | OAuth flow, broker impl, fill reconciliation |
| 5 — Hardening | Pending | Reconciliation drift alerts, error handling, journal export, signal-detail breakdown view |
