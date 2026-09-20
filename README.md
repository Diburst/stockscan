# Stockscan

Personal swing-trading scanner, backtester, and position manager.

> See [DESIGN.md](./DESIGN.md) for the full system design and [USER_STORIES.md](./USER_STORIES.md) for functional behavior.

## Status

**Phases 0–3 complete plus a substantial set of feature additions.** What works today:

- **Data layer**: TimescaleDB hypertable for bars, idempotent ingest, EODHD client (per-symbol + bulk EOD + news endpoints), FRED client for macro series (HY OAS, Treasury yields), historical S&P 500 universe with survivorship-bias correction (Wikipedia fallback on prices-only plans)
- **Strategy plugin system**: drop a Python file in `strategies/`, restart, it's live. Every knob is a class constant on the strategy (`Strategy.knobs()` lists them); edit the file and bump the version to change one
- **Two strategies, one book**: `rsi2_meanrev` (RSI(2) pullback in an uptrend, sector-relative ranking, quiet-volume filter, no price stop, fixed 10% of equity per position) and `momentum_52w_high` (Stage-2 eligibility, 52-week-high closeness + Clenow slope quality + sector-residual tilt, weekly review, 15% stop / SMA(100) break / 85%-of-high exits, 0.75% risk, max 10 positions)
- **Market regime layer**: SPY 200-day trend gate with a 3-close dwell (blocks new entries only), realized-vol position scalar (top tercile of the trailing year, per-strategy opt-in), HY OAS credit-stress breaker. Labels `risk_on` / `risk_off` / `credit_stress`. The backtest engine applies the same rules from the same `regime_frame`
- **Sizing**: `size_for_strategy` shared by the live runner and the backtest engine — risk % against the strategy's stop, or a fixed fraction for stop-less strategies, times the vol scalar where the strategy opts in. Sector cap, ADV cap and per-strategy position cap all bind in both paths
- **Event-driven backtester** sharing strategy, sizing and regime code with the live engine; metrics module; `backtest debug` replays `signals()` day by day for one symbol
- **Web UI** (mobile-first responsive): top nav is Dashboard, Signals, Watchlist, Options, Hedge, Trades, Backtests, with Strategies and Docs in the footer. Dashboard with regime card (trend gate / vol scalar / credit stress + per-strategy sizing lines), latest-scan passing signals + news card, Signals (with Fetch Latest + freshness chip), Signal detail (full attribution: outcome, score derivation, sizing breakdown, regime context, strategy version), Watchlist, Trades, Backtests, Base-Rate Analyzer, Strategies (sizing rule + tuning knobs per strategy), per-symbol Analysis (trend, volatility, earnings, insider activity, options context — reached from any symbol link or the Watchlist's Analyse button)
- **In-app news reader**: Dashboard news card with per-article expand-on-click, on-demand re-fetch from EODHD (not persisted, no content-rights concerns)
- **Watchlist** with per-symbol price-target alerts (above/below), auto-disable after firing, "+ Watch" quick-add from Dashboard
- **Strategy-owned signal scores**: each strategy computes its own ranking score and persists the inputs behind it in `signals.metadata`; the signal-detail page renders them with trader-language labels
- **Fundamentals layer**: 38 typed columns + JSONB raw payload from EODHD, point-in-time shares history for the sector composites, market-cap percentile helper
- **Sector composites**: equal-weight sector indices rebuilt nightly from bars; both strategies rank against them
- **Notifications**: email (SMTP / Postmark) + Discord webhook, fired by the nightly job; step failures are carried into the summary (subject gains a DEGRADED tag)
- **Scheduler**: Docker Compose `scheduler` service (supercronic + `infra/crontab`) on any host, or launchd plist templates on macOS — nightly-scan, daily DB backup, weekly fundamentals refresh
- **Deployment**: full Docker Compose stack (db + migrate + web + scheduler) — see [DEPLOY.md](./DEPLOY.md); self-hosted UI assets (no CDN dependency)
- **Logging**: central setup with per-component rotating files under `logs/` (web / cli / nightly), request-timing middleware with slow-request warnings
- **Migration runner**: 26 SQL migrations, custom runner (replaces Alembic, AUTOCOMMIT-aware)
- **MCP server** (`stockscan mcp serve`): signals, watchlist, regime, backtests and options tools for AI agents — see [MCP_SERVER.md](./MCP_SERVER.md)

Pending: **Phase 4** (E*TRADE OAuth + broker integration), **Phase 5** (reconciliation drift detection, journaling polish), the **settling backtests** and the **strategy-optimizer** item in `TODO.md`.

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
- `EODHD_FEATURES` — only if you're **not** on the All-In-One plan. `eod,bulk` for the prices-only plan; see [Data plans](#data-plans-eodhd_features) below.

Optional settings worth knowing (all have defaults in `config.py`):

| Variable | Default | What it does |
|---|---|---|
| `FRED_API_KEY` | empty | Enables the FRED macro refresh (HY OAS for the credit-stress breaker, Treasury yields for the options analysis). Without it the credit-stress flag stays off. |
| `STOCKSCAN_STARTING_EQUITY` | `100000` | Equity the live scanner sizes against until the first `equity_history` row exists (fresh install, paper trading without a broker sync). |
| `STOCKSCAN_MAX_POSITIONS` | `15` | Portfolio-wide open-position cap (each strategy may also set its own `max_open_positions`). |
| `STOCKSCAN_MAX_POSITION_PCT` / `STOCKSCAN_MAX_SECTOR_PCT` / `STOCKSCAN_MAX_ADV_PCT` | `0.08` / `0.25` / `0.05` | Notional caps per position, per sector, and versus 20-day dollar volume. |
| `STOCKSCAN_DRAWDOWN_CIRCUIT_BREAKER` | `0.15` | No new entries once equity is this far below its high-water mark. |

Risk per trade is **not** an environment variable — it lives on each strategy (`default_risk_pct` or `position_pct`).

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
make test            # 700+ tests should pass
make db-status       # should show migrations 0001..0026 applied, no pending
```

---

## Deploying with Docker (any host)

The bare-metal setup above is the macOS dev path. For deployment — to a
Linux box, a NAS, a VPS, or even the same Mac — the whole stack
(TimescaleDB + migrations + web UI + scheduled jobs) runs from one command:

```bash
cp .env.example .env       # set STOCKSCAN_DB_PASSWORD + EODHD_API_KEY
docker compose up -d --build
```

See [DEPLOY.md](./DEPLOY.md) for the full walkthrough: seeding data,
restoring an existing database, day-2 operations, and the scheduler
(`infra/crontab` replaces launchd on non-Mac hosts).

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
| `/` | Dashboard — equity, the latest scan's passing signals (with "+ Watch" quick-add), open positions, **Market Regime** card showing the trend gate (with days on side), the vol scalar (realized vol + percentile rank) and the credit-stress flag, each with a dropdown explanation, plus a per-strategy line saying its sizing rule and whether the vol scalar applies, **news card** with per-article expand-on-click reader |
| `/signals` | Today's passing + rejected signals, filterable by strategy. **Header strip**: "Last scan: Xh ago · N today" + "Bars current through: YYYY-MM-DD [fresh/Nd behind]" + ⟳ Fetch Latest button (HTMX-swapped: backfills 7 days of bars + re-runs every strategy). **Score** column (the strategy's own ranking metric — idiosyncratic drop for RSI(2), closeness + slope quality + residual tilt for momentum — with the inputs on the detail page) |
| `/signals/{id}` | Full signal attribution: Outcome (entry/stop/qty/risk-per-share/notional — or "no stop" for stop-less strategies), Score derivation (humanized strategy metadata with one-line tooltips per input), Position sizing (the strategy's rule × the vol scalar where it applies, plus the trend gate and credit-stress state), Market regime context (gate, SPY vs SMA(200), realized vol + rank, HY OAS + rank), Strategy version at scan time |
| `/signals/{id}/base-rates` | Historical-setup outcome stats for that strategy on that symbol |
| `/news/{article_id}/content` | HTMX fragment endpoint — re-fetches the article body from EODHD on demand (not persisted) |
| `/watchlist` | Watched symbols with last close, % change, volume, price target editor, alert toggle, and the sector-composite chart |
| `/trades` | Open + closed trades; round-trip stats |
| `/trades/{id}` | Single-trade detail with notes thread (markdown + FTS) |
| `/backtests` | Saved backtest runs (with CAGR + Sharpe). **Run a backtest** form at the top — same inputs and defaults as `stockscan backtest run`; the run starts in the background, the page polls until it lands, and the result appears in the list |
| `/backtests/{id}` | Run detail with equity curve + trade log |
| `/strategies` | Registered strategies with descriptions and each card's sizing rule (risk % against the stop, or a fixed fraction per position, and whether it is vol-scaled) |
| `/strategies/{name}` | Strategy detail with the sizing summary (rule, max open positions, vol scalar applies or not), the rendered manual, the **tuning knobs** table read off the class, and the freshness of any non-bar inputs (e.g. latest sector-composite bar) |
| `/analysis` · `/analysis/{symbol}` | Per-symbol analysis: trend bucket (MA stack + returns), realized-volatility state, options context (Black-Scholes strike framing), insider activity |
| `/health` | JSON status (DB, TimescaleDB extension, registered strategies) |
| `/docs` | **Documentation hub** — index of all repo markdown docs (README, DESIGN, USER_STORIES, TODO, DEPLOY, MIGRATION, regime-research) plus the auto-generated CLI reference. Renders markdown with TOC + anchor links; CLI reference walks the live Typer command tree |
| `/docs/cli` | Auto-generated CLI reference. Captures `--help` for every `stockscan` command/group/leaf via `typer.testing.CliRunner` — single source of truth, never drifts |
| `/docs/{slug}` | Renders one of the registered markdown files (slugs: `readme`, `design`, `user-stories`, `todo`, `deploy`, `migration`, `regime-research`) |
| `/api-docs` | FastAPI's auto-generated Swagger UI (relocated from `/docs`) |
| `/api-redoc` | FastAPI's ReDoc alternative |
| `/api-openapi.json` | OpenAPI JSON spec |

Mobile-first responsive throughout — tables collapse to cards below 640px, modals become full-screen routes (notably the trade ticket).

---

## Data plans (`EODHD_FEATURES`)

Everything the scanner, backtester, regime engine and nightly job need is
**price data** — per-symbol `/eod` and the bulk endpoint — which the
cheapest EODHD plan ("EOD Historical Data — All World") includes. The other
endpoint families are extras: fundamentals (market cap, sector), news,
earnings/econ calendars, insider transactions — and, less obviously, the
**index constituents** used by `refresh universe`, which come from
`/fundamentals/GSPC.INDX` and therefore need the Fundamentals plan.

`EODHD_FEATURES` in `.env` declares what your plan includes. Default `all`.

| Plan | Setting |
|---|---|
| All-In-One | `EODHD_FEATURES=all` (or unset) |
| EOD Historical Data — All World (prices only) | `EODHD_FEATURES=eod,bulk` |
| EOD + Fundamentals | `EODHD_FEATURES=eod,bulk,universe,fundamentals` |

Valid names: `eod`, `bulk`, `universe`, `fundamentals`, `news`, `calendar`,
`insider`, `econ_events`. With a family excluded:

- `stockscan refresh fundamentals` / `refresh news` print a one-line notice and exit 0 (so the Sunday fundamentals cron and any scripts keep working), and the watchlist / analysis / MCP refresh paths skip that leg — **no request is made**, nothing counts against quota, no `DEGRADED` nightly summaries.
- Stored rows are still shown everywhere (the news card, insider tables, fundamentals). Refresh buttons become a muted "not available on current data plan" note.
- The sector composites both strategies rank against keep building from bars, but their sector map comes from the frozen `fundamentals_snapshot`; each strategy page shows the latest composite bar date.
- `stockscan refresh universe` switches to a **Wikipedia fallback** (see below) so newly added index members still get scanned.
- `stockscan health` lists the enabled families; startup logs a warning naming the excluded ones.

Nothing is deleted or migrated. Upgrading later is `EODHD_FEATURES=all` + restart.

## What does `stockscan refresh` actually fetch?

### `stockscan refresh universe`

One EODHD API call to `/fundamentals/GSPC.INDX` (Fundamentals plan). Pulls and persists into `universe_history`:

- **Current S&P 500 members** — ~500 symbols
- **Historical members back to ~2000** — every symbol ever in the index, with `joined_date` and `left_date`. Total ~1,200–1,500 unique symbols across history.

Run **weekly** — the index turns over slowly. Required before any `refresh bars` or backtest.

**Without the `universe` feature** (prices-only plan) the same command reads
Wikipedia's *List of S&P 500 companies* page instead — one HTTP request, no
EODHD quota. It is deliberately incremental: new members on the roster get
an open interval (dated from the page's "Date added" or the changes log),
members that have left get their open interval closed, and the EODHD-sourced
history back to ~2000 is left untouched. Share classes are normalised to the
EODHD form (`BRK.B` → `BRK-B`) so symbols line up with the `bars` table. A
roster that parses to fewer than 400 names is refused rather than merged,
so a page-layout change can't silently close hundreds of live intervals.

### `stockscan refresh bars`

Backfills daily OHLCV bars from the provider into the local store. Two modes:

**Per-symbol** — pass one or more tickers when you want bars for a single
name (e.g. a stock you're adding to the watchlist for technical analysis,
or a cash index like VIX) without touching the rest of the universe:

```bash
stockscan refresh bars AAPL                 # one ticker, default 2007→today
stockscan refresh bars AAPL MSFT NVDA       # several at once
stockscan refresh bars AAPL --start 2015-01-01
stockscan refresh bars VIX --exchange INDX  # cash index
```

No strategy runs, no signals are generated — this command only writes
OHLCV rows to the `bars` table. The watchlist UI, the per-symbol
technical analysis (`/analysis`), and any `backtest run` invocation that
references the symbol all read straight from this table.

**Universe-wide** — omit positional args to backfill every symbol ever in
the S&P 500 (current + historical members). Restoring delisted members
eliminates survivorship bias on backtests.

Defaults:

- **Symbols:** all symbols ever in the S&P 500 (current + historical, ~1,200–1,500 names). Use `--current-only` to fetch just the current ~500.
- **Start date:** 2007-01-01 (override with `--start YYYY-MM-DD`)
- **End date:** today (override with `--end YYYY-MM-DD`)
- **Interval:** daily

All invocations are **incremental** on re-run: per symbol, only the
window from `last_cached_date - 5 days` to `end` is re-fetched, so a
daily refresh after the initial backfill takes seconds.

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
uv run stockscan refresh bars AAPL               # one symbol (for watchlist / analysis)
uv run stockscan refresh bars AAPL MSFT NVDA     # several symbols at once
uv run stockscan refresh bars VIX --exchange INDX  # cash indices
uv run stockscan refresh bars                    # full universe (~6M bars, 2007+)
uv run stockscan refresh bars --current-only     # current 500 only (~2M bars)
uv run stockscan refresh daily --days 5          # bulk-refresh recent N days
uv run stockscan refresh fundamentals --current-only  # ~500 EODHD fundamentals calls
uv run stockscan refresh macro                   # FRED HY OAS + 1M/3M Treasury yields (regime breaker, options context)
uv run stockscan refresh macro BAMLH0A0HYM2 BAMLC0A0CMEY  # multiple FRED series
uv run stockscan refresh news                    # EODHD news for general feed + watchlist

# Inspect
uv run stockscan health                          # DB + extension + strategies
uv run stockscan strategies list
uv run stockscan strategies show rsi2_meanrev
uv run stockscan strategies show momentum_52w_high

# Scanning (live signals into DB)
uv run stockscan scan run rsi2_meanrev           # one strategy, today
uv run stockscan scan run --all                  # every registered strategy
uv run stockscan scan run rsi2_meanrev --as-of 2024-03-15   # backdated

# Signals backfill (replay scans — version-aware skip-query, so a version bump
# automatically re-scans older-version dates without --force)
uv run stockscan signals backfill momentum_52w_high                 # 1yr daily, resumable
uv run stockscan signals backfill all --start 2024-01-01             # all strategies, custom range
uv run stockscan signals backfill rsi2_meanrev --every 5             # weekly only
uv run stockscan signals backfill momentum_52w_high --force          # ignore skip set entirely

# Signals admin (delete prior-version data after a strategy upgrade)
uv run stockscan signals delete -s momentum_52w_high -v 1.0.0        # confirm interactively
uv run stockscan signals delete -s momentum_52w_high -v 1.0.0 --yes  # script-friendly
uv run stockscan signals delete -s rsi2_meanrev -v 1.0.0 \
    --start 2020-01-01 --end 2023-12-31                              # bounded date range

# Watchlist
uv run stockscan watchlist list
uv run stockscan watchlist add AAPL --target 200 --direction above
uv run stockscan watchlist remove 3
uv run stockscan watchlist check-alerts          # fire any pending now

# Backtesting (point-in-time S&P 500 by default; 5 bp slippage; $100k)
uv run stockscan backtest run rsi2_meanrev --from 2010-01-01
uv run stockscan backtest run momentum_52w_high --from 2010-01-01 -s AAPL -s MSFT
uv run stockscan backtest list --strategy rsi2_meanrev
uv run stockscan backtest debug rsi2_meanrev AAPL --from 2024-01-01  # per-day signals() replay
uv run stockscan backtest export 12 --out bt12.json                 # trades + equity + regime overlay
uv run stockscan backtest profile momentum_52w_high                 # cProfile hotspots (see DESIGN §4.4.1)

# Sector composites (rebuilt nightly; strategies rank against them)
uv run stockscan composites build                                    # full rebuild from 2007
uv run stockscan composites symbol AAPL                              # which composite a symbol maps to

# Scheduled jobs (run by supercronic / launchd in production)
uv run stockscan jobs nightly-scan               # bars → macro → regime → composites → scans → alerts → summary

# Web + tests
make run-web                                     # FastAPI dev server on :8000
make test                                        # unit tests (700+)
make check                                       # lint + typecheck + test
```

---

## Project layout

```
stock-scan/
├── DESIGN.md                # System design (authoritative)
├── USER_STORIES.md          # Functional spec
├── market_regime_detection.md  # Regime layer design note (evidence + rules)
├── TODO.md                  # Backlog + settling backtests
├── README.md
├── pyproject.toml
├── Makefile
├── Dockerfile               # App image (web + scheduler) — see DEPLOY.md
├── docker-compose.yml       # Full stack: db + migrate + web + scheduler
├── tailwind.config.js       # Theme for the built stylesheet (`make css`)
├── migrations/              # Plain SQL (custom runner; no Alembic) — 26 files
│   ├── 0001_initial_schema.sql        # bars, accounts, signals, trades, lots, notes ...
│   ├── 0002_backtest_tables.sql       # backtest_runs / trades / equity_curve
│   ├── ...                            # watchlist, fundamentals, news, sectors, options, hedge ...
│   ├── 0025_regime_v3.sql             # trend gate + vol scalar + credit breaker columns
│   └── 0026_paper_trades_optional_stop.sql  # latest — `ls migrations/` for the full story
├── infra/
│   ├── docker-compose.yml             # TimescaleDB
│   ├── setup_db.sh
│   ├── scripts/
│   │   └── db_backup.sh               # pg_dump rotation
│   ├── crontab                        # compose scheduler jobs (supercronic, ET times)
│   ├── launchd/                       # plist templates (macOS path): nightly-scan, web, db-backup
│   │   └── INSTALL.md
│   └── docs/
│       └── mobile-setup.md
├── src/stockscan/
│   ├── cli.py                         # `stockscan ...` (db / refresh / scan / backtest /
│   │                                    watchlist / jobs / strategies / signals / analysis /
│   │                                    composites / options / hedge / mcp)
│   ├── config.py                      # Pydantic settings
│   ├── db.py                          # SQLAlchemy engine + healthcheck
│   ├── db_migrate.py                  # SQL migration runner (Alembic replacement)
│   ├── tables.py                      # SQLAlchemy Core table definitions
│   ├── metrics.py                     # CAGR, Sharpe, Sortino, max DD, profit factor
│   ├── data/                          # Provider clients (EODHD + FRED + stub), store, backfill,
│   │                                    macro_store + macro_refresh (FRED series)
│   ├── universe/                      # S&P 500 membership (EODHD sp500.py, wikipedia.py fallback)
│   ├── fundamentals/                  # Snapshot store + EODHD refresh + market_cap_percentile
│   ├── sectors/                       # Equal-weight sector composites
│   ├── indicators/                    # ta.py (sma/ema/rsi/atr/true_range/ADV/Yang-Zhang vol),
│   │                                    relative_strength.py (sector_return, sector_relative_return)
│   ├── strategies/                    # base.py (ABC + registry + knobs) + rsi2_meanrev + momentum_52w
│   ├── regime/                        # rules.py (pure math, regime_frame) + detect.py + store.py
│   ├── analyzer/                      # Per-signal historical base-rate analysis
│   ├── analysis/                      # Per-symbol trend / volatility / options context
│   ├── scan/                          # ScanRunner + signals_freshness + refresh_signals (Fetch Latest)
│   ├── risk/                          # sizer.py (size_for_strategy) + filters.py (filter chain)
│   ├── broker/                        # Broker ABC + Suggestion + Paper (E*TRADE in Phase 4)
│   ├── backtest/                      # Event-driven engine + slippage + persistence + profile
│   ├── positions/, paper_store/       # Trade lifecycle helpers, paper trades
│   ├── notes/                         # Trade notes CRUD + FTS search
│   ├── news/, earnings/, insider/,    # EODHD extras: news reader, earnings calendar,
│   │   econ_events/                     insider transactions, economic events
│   ├── proposals/, hedge/, cycles/    # Options proposals, delta hedging, cycle tools
│   ├── watchlist/                     # Store + alerts + nightly hook
│   ├── notify/                        # Email (SMTP) + Discord webhook + router
│   ├── jobs/                          # Nightly orchestration
│   ├── mcp/                           # MCP server (see MCP_SERVER.md)
│   └── web/                           # FastAPI app, routes, Jinja templates (mobile-first),
│                                        self-hosted static assets (Tailwind build + htmx)
└── tests/                             # 700+ tests
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
| 1 — Strategies + backtester | ✅ Done | Indicator primitives, RSI(2) pullback, 52-week-high momentum, event-driven backtester, metrics, CLI |
| 2 — Web UI | ✅ Done | Dashboard, Signals, Trades, Backtests, Base-rates, Strategies — mobile-first responsive |
| 3 — Live scanner + notifications | ✅ Done | Bulk EOD endpoint, scheduler (launchd / supercronic), nightly job, email + Discord, db-backup |
| Watchlist | ✅ Done | Per-symbol price-target alerts, auto-disable on fire, "+ Watch" quick-add, Discord/email alerts via nightly hook |
| Fundamentals layer | ✅ Done | EODHD refresh, 38 typed columns + JSONB raw, point-in-time shares history, market_cap_percentile helper |
| Sector composites | ✅ Done | Equal-weight sector indices from bars + sector map; rebuilt nightly; `sector_return` / `sector_relative_return` primitives |
| Market regime | ✅ Done | SPY 200-day trend gate with dwell, realized-vol scalar with per-strategy opt-in, HY OAS credit-stress breaker. FRED provider + `macro_series`. Same `regime_frame` in live and backtest. Dashboard card with per-control explanations |
| News integration | ✅ Done | EODHD `/news` for general feed + watchlist symbols, sentiment-aware ranking, dashboard card with **on-demand article reader** (per-row expand → re-fetch from provider, never persisted), CLI `refresh news` |
| Strategy canon review (2026-09) | ✅ Done | Book reduced to RSI(2) pullback + 52-week-high momentum, both v2.0.0; knobs as class constants; exits (stops included) strategy-owned; sizing shared by runner and engine. Settling backtests listed in `TODO.md` |
| Signal-detail full attribution | ✅ Done | Outcome, Score derivation (humanized strategy metadata + tooltips), Position sizing (strategy rule × vol scalar, gate + credit-stress state), Market regime context (gate, realized vol + rank, HY OAS + rank), Strategy version at scan time, raw JSONB fallback |
| Signals freshness + Fetch Latest | ✅ Done | Header strip on `/signals` showing last scan + bars-current-through with [fresh/Nd behind] badge. POST `/signals/refresh` button: 7-day bulk-EOD bars catch-up + re-runs every registered strategy via HTMX |
| Options + hedging | ✅ Done | Weekly short-premium proposals (`stockscan options propose`), delta-hedge daemon and playground (`/hedge`), MCP tools for both |
| 4 — E*TRADE integration | Pending | OAuth flow, broker impl, fill reconciliation |
| 5 — Hardening | Pending | Reconciliation drift alerts, error handling, journal export |
| Settling backtests | Pending | Ablations for both strategies and the regime controls — see [TODO.md](TODO.md) |
| Strategy optimizer (Bayesian) | Pending | See [TODO.md §High-impact](TODO.md). Walk-forward + held-out validation + deflated Sharpe + per-trial persistence |
