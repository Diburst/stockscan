# User Stories — Personal Stock Trading App

**Author:** Thomas
**Status:** Draft v0.3
**Date:** 2026-04-27
**Companion to:** [DESIGN.md](./DESIGN.md)

> **v0.3 changes:** Added three new stories that have shipped: **Watchlist** (Story 11) with per-symbol price-target alerts and one-click "+ Watch" from the Dashboard, **Technical Confirmation Score** (Story 12) — a strategy-aware signed bias derived from RSI + MACD, displayed alongside the strategy score on Signals and Watchlist pages, and **Fundamentals Refresh** (Story 13) which underpins the Largecap Rebound strategy's market-cap filter. Story 1's "Run scan" CLI snippet updated to match the live commands.

> **v0.2 changes:** Mobile/responsive UI elevated to a v1 requirement (was deferred). Phone access is via existing WireGuard VPN to the home LAN. Per-story mobile considerations added below; cross-cutting responsive design requirement added at the end.

This document captures the functional behavior of the system from the user's point of view — what the user does, what they see, what the system promises in return. DESIGN.md describes the architecture; this describes the experience.

The trader is the only user. "I" below means Thomas at the dashboard.

---

## Story Summary

| # | Story | Primary modules |
|---|---|---|
| 1 | Scan for one strategy | Scanner, UI |
| 2 | Scan for any/all strategies | Scanner, UI |
| 3 | Set up a trade from a scan result | Scanner → Sizer → Broker abstraction |
| 4 | Estimate likelihood of profit for a candidate | **Base-rate analyzer** (new), Backtester |
| 5 | Track entered/exited trades + stats + P&L | Position manager, Backtester (shared metrics) |
| 6 | Write manual notes per entry and exit | **Trade notes** (new) |

Plus supporting stories surfaced while writing the above:

| # | Story | Primary modules |
|---|---|---|
| 7 | View daily scan summary in email/Discord | Notifier |
| 8 | Record a manual fill when broker is offline | Position manager, Suggestion broker |
| 9 | Reconcile local positions against broker | Position manager |
| 10 | Check system health (data freshness, broker auth) | Status panel |
| 11 | Watch symbols and get price-target alerts | **Watchlist** (new) |
| 12 | See a per-signal Technical Score next to the strategy score | **Technical scoring** (new) |
| 13 | Refresh fundamentals to enable market-cap-aware strategies | **Fundamentals layer** (new) |

---

## Story 1 — Scan for ONE strategy

> **As Thomas, I want to scan the database for stocks that meet the entry criteria for one specific strategy, so I can find candidate trades for that strategy on demand.**

### Trigger
- Scheduled: `refresh-and-scan` job at 20:00 ET runs all strategies, but a single-strategy scan is also runnable from the UI or CLI.
- Manual: "Run scan" button on the strategy page, or `stockscan scan rsi2` from the CLI.

### Preconditions
- Bars are fresh through yesterday's close (status panel shows green data freshness).
- The strategy's parameters are configured.

### Happy path (UI)

```
┌────────────────────────────────────────────────────────────────────────────┐
│ Scan: RSI(2) Mean-Reversion                          [Run scan] [Settings] │
├────────────────────────────────────────────────────────────────────────────┤
│ As of:  2026-04-27 (close)         Universe: S&P 500 (501 names)           │
│                                                                            │
│ Passing signals (3)                                                        │
│ ─────────────────────────────────────────────────────────────────────────  │
│ Symbol  Score  Close   RSI(2)  Stop    Suggested qty   $ at risk   ▣ Chart│
│  AAPL    0.92  189.40   3.1   181.20   54              $9,936       ▢      │
│  MRK     0.81  104.55   5.8   100.10   168             $7,476       ▢      │
│  CVS     0.74   58.32   8.2    55.40   685             $2,000       ▢      │
│                                                                            │
│ Rejected (5) ▾                                                             │
│ ─────────────────────────────────────────────────────────────────────────  │
│ Symbol  Score  Reason                                                ▣     │
│  PFE     0.88  ⚠ Earnings in 3 days                                  ▢     │
│  KO      0.76  ⚠ Sector cap (Consumer Staples at 24.8%, +KO=27.1%)   ▢     │
│  ABBV    0.71  ⚠ Position size > 5% of 20d ADV                       ▢     │
│  BAC     0.65  ⚠ Already long via TF strategy                        ▢     │
│  WMT     0.62  ⚠ Below 200 SMA filter                                ▢     │
│                                                                            │
│ [⚙ Backdate scan to: 2024-03-15]   [📋 Export CSV]   [✉ Email summary]    │
└────────────────────────────────────────────────────────────────────────────┘
```

### Acceptance criteria

1. Scan completes in under 30 seconds for the S&P 500 against the local DB.
2. Each row shows: symbol, score, current close, strategy-specific indicator value(s), suggested stop, suggested qty (from sizer), dollar risk, and a hover-or-click chart preview.
3. **Rejected signals are shown alongside passing ones, visually distinguished**, with the specific filter that blocked them.
4. The user can re-run the same scan against any historical date — `as_of` is a parameter, not a constant. Rejection reasons are evaluated as of that date too.
5. Output is persisted to `signals` and `strategy_runs` tables; status is `'new'` for passing, `'rejected'` for failed-with-reason.
6. A button exports the current view to CSV.
7. Result is reproducible: re-running with the same `as_of` and unchanged params produces identical output (deterministic).

### Edge cases

- **Stale data:** if any symbol's last bar is older than the previous trading day, scan still runs but flags the affected symbols with a "stale" badge.
- **Missing data for a symbol:** symbol is excluded from the scan and listed in a "skipped" row with reason "no bars".
- **All signals rejected:** the passing section shows "No qualifying signals" with a "show rejections" prompt.
- **Backdated scan against a date the symbol wasn't in the index:** symbol is excluded; the universe respects historical S&P 500 membership.
- **Strategy params changed since last run:** historical scan results in DB remain immutable; new run gets a new `run_id`.

### Module mapping
- `stockscan.scan` (single-strategy scan entry point).
- `stockscan.universe` (provides as-of-date membership).
- `stockscan.risk` (sizer + filter chain — populates rejection reasons).
- `stockscan.web.signals` (page renders results).

### Mobile

The 8-column desktop table collapses to a stacked card per signal. Card layout:

```
┌──────────────────────────────────────┐
│ AAPL              0.92    [Trade ▶]  │
│ $189.40    RSI(2) 3.1                │
│ Stop $181.20 · 54 sh · $9,936 risk   │
│ ✓ Passing                            │
└──────────────────────────────────────┘
┌──────────────────────────────────────┐
│ PFE  ⚠ Rejected   0.88               │
│ Earnings in 3 days                   │
│ (tap to view full signal)            │
└──────────────────────────────────────┘
```

The "Backdate scan" control becomes a date picker in a collapsible "Scan options" panel. Filters and exports live behind a toolbar icon.

---

## Story 2 — Scan for ANY strategy

> **As Thomas, I want to run all strategies at once and see the combined opportunity set, so I can make my daily decisions in one place.**

### Trigger
- Scheduled at 20:00 ET as part of `refresh-and-scan`.
- Manual: "Run all" button on the dashboard, or `stockscan scan --all`.

### Happy path (UI)

```
┌────────────────────────────────────────────────────────────────────────────┐
│ Daily scan — 2026-04-27                                  [Run all scans]   │
├────────────────────────────────────────────────────────────────────────────┤
│ ▼ RSI(2) Mean-Reversion        3 passing,  5 rejected                      │
│   AAPL   0.92   $9,936 risk    🔗 Also flagged by: (none)                   │
│   MRK    0.81   $7,476 risk    🔗 Also flagged by: (none)                   │
│   CVS    0.74   $2,000 risk    🔗 Also flagged by: Donchian (long)          │
│                                                                            │
│ ▼ Donchian Trend                1 passing,  2 rejected                     │
│   CVS    0.69   $5,810 risk    🔗 Also flagged by: RSI(2) (long)  ✓ aligned │
│                                                                            │
│ ▼ Cross-strategy conflicts (0)                                             │
│   (none today)                                                             │
└────────────────────────────────────────────────────────────────────────────┘
```

### Acceptance criteria

1. Each strategy gets its own collapsible section with the same columns as Story 1.
2. **Symbols appearing in multiple strategies show a "🔗 Also flagged by" badge** linking to the other row(s).
3. **Cross-strategy *conflicts* are surfaced in a dedicated panel** — e.g., one strategy says long but another says exit on the same name (only relevant once we hold the position). Conflicts are flagged but neither signal is suppressed.
4. The user can expand/collapse each strategy section independently.
5. The combined run writes one `strategy_runs` row per strategy, all sharing a parent `daily_run_id` for navigation.

### Edge cases

- **One strategy errors:** other strategies still complete; the failing one shows an error banner with a stack-trace link.
- **No signals across all strategies:** dashboard shows a "Quiet day" message — useful information, not an empty state.

### Module mapping
- `stockscan.scan.runner` (orchestrates parallel strategy scans).
- `stockscan.web.dashboard` (combined view).

---

## Story 3 — Set up a trade from a scan result

> **As Thomas, I want to turn a scan result into a real (or simulated) order with one click, with the sizer and stop pre-populated, so I don't have to re-do the math.**

### Trigger
- Click on a passing signal row in any scan view → a trade ticket modal opens.

### Happy path (UI)

```
┌────────────────────────── Trade Ticket ───────────────────────────┐
│ AAPL · long  · from RSI(2) Mean-Reversion · signal #4821          │
│                                                                   │
│ Strategy stop:    $181.20 (entry − 2.5×ATR)                       │
│ Suggested qty:    54 shares  (1% risk · $9,936)                   │
│ Notional:         $10,228                                         │
│                                                                   │
│ Order type:    [● Market on Open]  [○ Limit @ ___]  [○ Stop ___]  │
│ Time in force: [● DAY]  [○ GTC]                                   │
│ Quantity:      [ 54 ]   ← editable                                │
│                                                                   │
│ Broker: Suggestion Mode (E*TRADE auth lapsed — [reconnect])       │
│                                                                   │
│ Entry thesis (optional):                                          │
│ ┌───────────────────────────────────────────────────────────────┐ │
│ │ Pulling back to recent support after Q2 beat. Index uptrend   │ │
│ │ intact. RSI(2) at 3.1 is the lowest in 6 months.              │ │
│ └───────────────────────────────────────────────────────────────┘ │
│                                                                   │
│       [Cancel]   [Save as suggestion]   [Submit to broker]        │
└───────────────────────────────────────────────────────────────────┘
```

### Acceptance criteria

1. Ticket pre-fills `symbol`, `side`, `qty` (from sizer), `order_type` (default market-on-open), `stop_price` (from strategy), `time_in_force`.
2. User can edit qty, order type, limit/stop prices, and TIF before submitting.
3. **Entry thesis is captured at submit time** as the first note on the resulting trade (Story 6).
4. Submission routes through the active `Broker`:
   - `ETradeBroker` → API call → `broker_order_id` returned and stored.
   - `SuggestionBroker` → writes to `suggestions` table, sends email + Discord.
   - `PaperBroker` → simulated fill at next available bar.
5. The originating `signal.status` is updated to `'ordered'`.
6. A new row in `orders` is created, linked to the signal.
7. The broker dropdown shows current connection status; if E*TRADE auth has lapsed, the system silently routes to Suggestion Mode and shows a banner explaining why.

### Edge cases

- **Qty change crosses a constraint:** if the user edits qty above the position-size or sector cap, the submit button shows a warning ("⚠ This order exceeds your sector cap by $4k — confirm anyway?") with explicit confirmation.
- **Insufficient cash:** ticket shows a warning before submit; user can override or reduce qty.
- **Symbol price moved significantly since the signal:** if last price is >2% from `suggested_entry`, ticket shows a "stale signal" warning.
- **Already in position:** if a position already exists for this symbol/strategy, ticket shows the current position and asks "add to position" vs "cancel".
- **Broker rejects order:** error captured in `orders.status`, user notified via Discord, suggestion-mode fallback offered.

### Module mapping
- `stockscan.web.ticket` (modal + form handling; full-screen route on mobile).
- `stockscan.risk` (re-validates constraints at submit time).
- `stockscan.broker` (routes to active impl).
- `stockscan.notes` (captures entry thesis as first note).

### Mobile

The trade ticket is the highest-stakes mobile screen. On phones it renders as a **full-screen route** (`/ticket/<signal_id>`), not an overlay modal. Layout vertically stacked:

```
┌────────────────────────────┐
│ ◄ AAPL · long              │
│ from RSI(2) · signal #4821 │
├────────────────────────────┤
│ Stop:    $181.20           │
│ Suggested qty:   54        │
│ Notional:  $10,228         │
│ Risk:     $9,936 (1%)      │
│                            │
│ Order type                 │
│ ┌────────────────────────┐ │
│ │ Market on Open      ▾ │ │
│ └────────────────────────┘ │
│                            │
│ Quantity  [  54        ]   │
│ TIF       [ DAY      ▾ ]   │
│                            │
│ Broker: Suggestion Mode    │
│ ⓘ E*TRADE auth lapsed      │
│   [Reconnect]              │
│                            │
│ Entry thesis (optional)    │
│ ┌────────────────────────┐ │
│ │                        │ │
│ │                        │ │
│ └────────────────────────┘ │
│                            │
│ ┌──────────┐ ┌──────────┐  │
│ │  Cancel  │ │  Submit  │  │
│ └──────────┘ └──────────┘  │
└────────────────────────────┘
```

- Submit button is sticky at the bottom of the viewport so it's always reachable without scrolling.
- Numeric fields use `inputmode="decimal"` to trigger the number keyboard.
- Confirmations (sector cap warnings etc.) appear as full-width banners above the submit button.

---

## Story 4 — Estimate likelihood of profit (similar-setup base rates)

> **As Thomas, I want to see how this exact kind of setup has performed historically — including setups my filters would have rejected — so I can decide whether to take the trade and whether my filters are helping or hurting.**

This is the most analytically interesting story. The output is a base-rate dashboard for a single signal.

### Trigger
- Click "📊 Base rates" on any passing or rejected signal row → navigates to a per-signal analysis page.

### Definition of "similar setup"

For a given signal `(strategy, symbol, as_of_date)`, walk the historical bars and identify every prior date where the strategy's *entry rules* fired on the same symbol — independent of whether portfolio filters would have blocked it. Each such instance is a "historical setup". For each historical setup, simulate the strategy's exit rules and record the outcome.

This is a **strategy-on-symbol historical analysis, including filter-rejected instances**, with outcome statistics layered on top.

### Happy path (UI)

```
┌────────────────────────────────────────────────────────────────────────────┐
│ Base rates · AAPL · RSI(2) Mean-Reversion                                  │
│ Setup as of 2026-04-27: RSI(2)=3.1, Close > SMA(200), no earnings in 5d   │
├────────────────────────────────────────────────────────────────────────────┤
│ Historical instances on AAPL: 38 over 16 years                             │
│   • Would have passed all filters: 31                                      │
│   • Would have been rejected by filters: 7  (which?)                       │
│                                                                            │
│ Outcomes — passing setups (31)                                             │
│   Win rate:           71%  (22 wins, 9 losses)                             │
│   Avg holding period: 4.2 trading days                                     │
│   Avg win:            +2.4%                                                │
│   Avg loss:           −1.8%                                                │
│   Profit factor:      2.13                                                 │
│   Expectancy/trade:   +0.95%                                               │
│   Max excursion:      best +6.1%, worst −4.2%                              │
│   Distribution:  ████▆▅▃ ▁ ▁ ▁              (returns histogram)            │
│                                                                            │
│ Outcomes — rejected setups (7)                                             │
│   Win rate:           57%  (4 wins, 3 losses)                              │
│   Expectancy/trade:   −0.21%                                               │
│   Filters that rejected:  Earnings (4) · Sector cap (2) · ADV (1)          │
│   ⓘ Net: rejecting these setups improved expectancy by ~1.16%/trade.       │
│                                                                            │
│ Index regime context (passing setups)                                      │
│   In bull market (SPY > 200d):  85% win rate (n=20)                        │
│   In bear market:                40% win rate (n=10)                       │
│   ← current regime: BULL                                                    │
│                                                                            │
│ ⚠ Sample size caveats:  n=31 is borderline. Treat as directional, not gospel. │
└────────────────────────────────────────────────────────────────────────────┘
```

### Acceptance criteria

1. For any signal, the analyzer enumerates every historical date where the same strategy's entry rules matched the same symbol.
2. **Each historical setup is split into "would have passed filters" vs "would have been rejected"**, with rejection reasons aggregated.
3. Outcomes are simulated using the strategy's actual exit rules (consistent with the live and backtest engines — same code path).
4. Statistics shown: win rate, avg holding period, avg win, avg loss, profit factor, expectancy, max favorable/adverse excursion, return distribution.
5. **Filter-impact panel:** comparing passing vs rejected outcomes shows whether the filters added or destroyed expectancy.
6. **Regime split:** stats are also broken out by index regime (bull/bear via SPY > 200 SMA) so the user can see whether the strategy's edge is regime-dependent.
7. **Sample-size caveat** displayed when n < 50 for any cohort.

### Edge cases

- **Brand new symbol (recently added to index):** insufficient history; show "n=2, insufficient sample" and suggest looking at the strategy-wide base rates instead.
- **Historical instances with corporate-action complications:** include only if adjusted prices are clean; flag the count of skipped instances.
- **Symbol delisted mid-history:** instances after delisting date are absent; show note about the truncation.

### Module mapping
- `stockscan.analyzer.base_rates` (new module).
- Reuses `stockscan.strategies.*` (entry/exit rules).
- Reuses `stockscan.risk` (filter-rejection replay).
- Reuses `stockscan.backtest.metrics` (statistics).
- `stockscan.web.base_rates` (page).

### Mobile

Base-rate page becomes a vertically-stacked sequence of metric cards. The two cohorts (passing vs rejected) render as full-width sections you scroll between. The histogram becomes a horizontal bar chart sized to viewport width. The "filter impact" headline number stays prominent at the top:

```
┌────────────────────────────┐
│ AAPL · RSI(2)              │
│ Setup as of 2026-04-27     │
├────────────────────────────┤
│ ⓘ Filters added +1.16%/    │
│   trade vs no filters      │
├────────────────────────────┤
│ Passing setups (31)        │
│ Win rate        71%        │
│ Avg win        +2.4%       │
│ Avg loss       −1.8%       │
│ Profit factor   2.13       │
│ Expectancy     +0.95%      │
│ ▆▆▆▅▃▂▁▁▁▁▁ (returns)      │
├────────────────────────────┤
│ Rejected setups (7)        │
│ ...                        │
├────────────────────────────┤
│ Regime: BULL               │
│ Bull win rate    85%       │
│ Bear win rate    40%       │
└────────────────────────────┘
```

### Open question
Should the analyzer also support cross-symbol "similar setup" search? E.g., "find all RSI(2) setups across the entire S&P 500 history where RSI(2) ≤ 5 AND price > 200 SMA AND in a bull regime" and aggregate. More powerful, more work. Defer to v1.5 unless you want it now.

---

## Story 5 — Track trades, stats, P&L

> **As Thomas, I want a journal of every trade I've taken, with running performance stats per strategy and overall, so I can see how the system is doing.**

### Trigger
- Always-on: "Trades" page in the web UI.

### Happy path (UI)

```
┌────────────────────────────────────────────────────────────────────────────┐
│ Trades                              Filter: [All strategies ▾] [All time ▾]│
├────────────────────────────────────────────────────────────────────────────┤
│ Performance (since 2026-01-01)                                             │
│   Total P&L:         +$24,180  (+2.4%)                                     │
│   Win rate:          63%   (32 / 19)                                       │
│   Profit factor:     1.78                                                  │
│   Sharpe (ann'd):    1.4                                                   │
│   Max drawdown:      −4.1%   (3 weeks, recovered 2026-03-12)               │
│                                                                            │
│ By strategy                                                                │
│   RSI(2) Mean-Reversion   42 trades   68% win   +$15,200   PF 1.92        │
│   Donchian Trend           9 trades   33% win   + $8,980   PF 2.41        │
│                                                                            │
│ Open positions (3)                                                         │
│   AAPL  RSI(2)   54 sh @ 189.40   day 2 of 10   MFE +1.1%  MAE −0.4%      │
│   MRK   RSI(2)  168 sh @ 104.55   day 1 of 10   MFE +0.2%  MAE −0.6%      │
│   COST  Donch'  120 sh @ 712.30   day 19        MFE +4.8%  MAE −1.2%      │
│                                                                            │
│ Closed trades (51)  ▾                                                      │
│   Date       Sym  Strategy   Qty  Entry   Exit   Days  P&L     P&L%       │
│   2026-04-22 NVDA RSI(2)     12  892.10  908.50    3   +$197   +1.8%      │
│   2026-04-19 JPM  RSI(2)     78  201.40  198.20    5   −$250   −1.6%      │
│   ...                                                                      │
│                                                                            │
│ [📊 Equity curve]   [📋 Export]   [🔍 Search notes]                        │
└────────────────────────────────────────────────────────────────────────────┘
```

### Acceptance criteria

1. Per-strategy and overall stats: total P&L, win rate, profit factor, expectancy, Sharpe (annualized), Sortino, max drawdown + recovery time, trade count, avg holding period.
2. **MAE/MFE per trade**: maximum adverse and favorable excursions are recorded daily for each open position and frozen at close. Critical input for stop-tuning.
3. P&L is computed from `lot_sales` (realized) + mark-to-market on open `tax_lots` (unrealized).
4. Open positions show real-time-ish P&L (refreshed on each daily close), days held, MAE/MFE so far, and the strategy's current exit decision.
5. Filterable by strategy, date range, account (when multi-account lands), open/closed.
6. Equity curve view derived from `equity_history` (per-day NAV).
7. Closed trades listed newest-first with click-through to full trade detail (entry signal, all lots, sales, notes, base-rate analysis as taken at the time).

### Edge cases

- **Partial exits:** a trade with 3 sells over a week shows as one trade with 3 lot_sales; P&L is the sum.
- **Adds to position:** entering more shares of an existing strategy position creates a new lot, but the trade view groups them under the parent position.
- **Manual fills logged after broker outage:** appear in the journal with a "manual fill" badge.
- **Splits during holding:** quantity and cost basis adjusted; trade view shows a "split adjusted on YYYY-MM-DD" note.

### Module mapping
- `stockscan.positions` (lot tracking, aggregates).
- `stockscan.metrics` (shared with backtester).
- `stockscan.web.trades` (page).

### Mobile

The trades page is one of the most-checked screens (you'll want to glance at open positions from anywhere). On mobile:

- **Header card with the four numbers that matter most:** total P&L $ and %, win rate, profit factor, max DD. Everything else collapses behind a "Stats" expander.
- **Open positions render as cards** sorted by days-held descending, with current P&L prominent and MAE/MFE in smaller type below.
- **Closed trades infinite-scroll** with a date header sticky at the top of each visible group.
- **Filter toolbar** (strategy / date range / open vs closed) lives behind a filter icon in the header to save vertical space.
- **Tap any trade card** to open the full trade detail page (notes, lots, base-rate-as-taken).

---

## Story 6 — Manual notes per entry and exit

> **As Thomas, I want to capture my reasoning at entry and my retrospective at exit, with optional templated prompts to keep me disciplined, so I can learn from each trade.**

### Trigger
- At entry: a notes field appears in the trade ticket (Story 3) and is saved as the first note on the trade.
- At exit: when a trade closes (full exit of all lots), the trade detail page nudges with an exit-review form.
- Anytime: notes can be added or edited from the trade detail page.

### Happy path (UI)

```
┌─────────────────── Trade detail · AAPL #1247 (CLOSED) ─────────────────────┐
│ RSI(2) Mean-Reversion · entered 2026-04-15 · exited 2026-04-19 · +1.8%     │
├────────────────────────────────────────────────────────────────────────────┤
│ Notes (3)                                              [+ Add note]        │
│                                                                            │
│ ▼ 2026-04-15  Entry thesis                                                 │
│    Pulling back to recent support after Q2 beat. Index uptrend intact.     │
│    RSI(2) at 3.1 is the lowest in 6 months.                                │
│                                                                            │
│ ▼ 2026-04-17  Mid-trade observation                                        │
│    Bounced as expected day 2. Holding to plan.                             │
│                                                                            │
│ ▼ 2026-04-19  Exit review (templated)                                      │
│    What worked:    Entry timing was clean. RSI signal was strong.          │
│    What didn't:    Could have held one more day for ~+0.5% more.           │
│    Pattern noted:  Friday exits often early; consider hold-through-Mon.    │
│    Holding rating: 7/10 — followed plan but exited a touch early.          │
│                                                                            │
│ [🔍 Search notes]   [📋 Export trade journal]                              │
└────────────────────────────────────────────────────────────────────────────┘
```

### Acceptance criteria

1. Notes attach to the **trade** (round-trip object), not individual lots or signals — keeps everything for one round-trip in one place.
2. Each note has: `created_at`, `note_type` (`'entry'`, `'mid'`, `'exit'`, `'free'`), `body` (markdown), optional `template_fields` (JSONB).
3. **Optional templated prompts** at entry and exit:
   - Entry: "Thesis", "What invalidates this?", "Catalyst (if any)".
   - Exit: "What worked", "What didn't", "Pattern noted", "Holding rating (1–10)".
   - Filling them is a click-skip; no enforcement.
4. Markdown rendered (links, lists, code blocks); image embedding via paste or drag.
5. **Postgres full-text search** across all notes (`tsvector` index on `body`).
6. Notes are tagged with auto-extracted keywords (basic — pull obvious nouns) for quick filtering.
7. Exit-review prompt fires once when the position fully closes; user can dismiss permanently per-trade.
8. Read-only export of all notes for a trade or date range as Markdown for external journaling.

### Edge cases

- **Multi-leg / partial exits:** mid-trade notes capture observations between partials; the exit-review template fires on the final exit.
- **Deleted trade (manual cleanup):** notes archived with the trade; not deleted.
- **Long notes / images:** note body is `TEXT` (no length limit in Postgres); images stored on disk (`~/data/notes/`) and referenced by relative URL.

### Module mapping
- `stockscan.notes` (new module).
- `trade_notes` table (added to schema in DESIGN.md v0.4).
- `stockscan.web.trade_detail` (page).

### Mobile

Notes are the workflow most likely to be used from a phone — taking a quick observation mid-trade or doing the exit retro from anywhere. Mobile design priorities:

- **Add-note action is one tap from the trade detail page** (sticky FAB at bottom-right, or always-visible "+ Note" button in the trade header).
- **Markdown editor on mobile = single textarea + toggle button** ("Write" / "Preview"). No split view; no toolbar (which is awkward on phones). Markdown still renders correctly when read back.
- **Templated prompts on mobile** stack each field as its own labeled textarea, with the "Skip template, write free-form" link at the top.
- **Voice-to-text:** the textarea uses standard mobile dictation (works out of the box on iOS/Android keyboards) — no additional integration needed.
- **Search across notes** works on mobile via a search field in the journal header; results are stacked cards with a snippet around the matched term.

---

## Supporting Stories (Surfaced While Writing)

### Story 7 — Daily scan summary in email/Discord

> **As Thomas, when I check my phone in the evening, I want a concise summary of today's scan so I can decide what to act on tomorrow morning.**

- Template: top 5 passing signals across all strategies, their dollar-risk, the chart link, and a 1-line "system health" note (data freshness, broker auth status, NAV vs HWM).
- Sent at 20:15 ET after the scan completes. No email if zero signals (configurable).
- Discord post is a richer card per signal with embedded chart thumbnail.

### Story 8 — Record a manual fill when broker is offline

> **As Thomas, when I've executed a trade in the E*TRADE web UI directly (because the API auth lapsed and I didn't want to wait), I need to tell the system about it so my position manager stays accurate.**

- "Log manual fill" button on any open suggestion or unfilled order.
- Form: qty, price, datetime, commission. Creates an `order` row marked `source='manual'` and a `tax_lot` for buys / `lot_sale` for sells.
- Reconciliation will catch this on the next pass and link it to the broker's actual order if available.

### Story 9 — Reconcile local positions against broker

> **As Thomas, I want the system to detect when its view of my positions diverges from the broker's, so I'm never operating on stale data.**

- Runs at 16:05 ET daily (and on demand from the dashboard).
- Diff: per-symbol qty + cost basis, local vs broker.
- On drift, sends Discord alert with the diff and a "review" link.
- Common causes: dividends, stock splits, manual trades not logged, partial fills.

### Story 10 — System health panel

> **As Thomas, I want a single glance to tell me whether the system is healthy enough to trust.**

```
┌── System status ────────────────────────────────────────────┐
│ ● Data freshness:   ✓ Bars current as of 2026-04-27 16:00   │
│ ● Earnings cal:     ✓ Refreshed 2026-04-27 06:00            │
│ ● Universe:         ✓ S&P 500 list current (501 names)      │
│ ● Broker (E*TRADE): ⚠ Token expires in 4h 12m  [Reconnect]  │
│ ● Last reconcile:   ✓ 2026-04-27 16:05  (no drift)          │
│ ● DB backups:       ✓ Last successful 2026-04-27 02:00      │
│ ● Disk free:        ✓ 412 GB / 500 GB                       │
└─────────────────────────────────────────────────────────────┘
```

A red light here is a "do not trade" signal. The morning order-placement job refuses to fire if any critical row is red.

---

## Story 11 — Watch symbols and get price-target alerts

> **As Thomas, I want to add specific symbols to a watchlist with optional "alert when price crosses X" rules, so I get notified about names I'm following without having to log in to check.**

### Trigger
- Search and add from the **Watchlist** tab directly, or
- One-click **+ Watch** button on Dashboard rows (signals + open positions).

### Happy path (UI)

```
┌────────────────────────────────────────────────────────────────────────────┐
│ Watchlist                                                                  │
├────────────────────────────────────────────────────────────────────────────┤
│ Add: [ AAPL ] [target $190.00] [above ▾]                       [Add]      │
│                                                                            │
│ Symbol  Last close  Δ        Volume      Tech     Target           Alert   │
│ ──────  ──────────  ──────   ─────────   ──────   ───────────────  ─────   │
│ AAPL    $189.40     +0.84%   52,488,700  +0.42    above $190.00  ☑       × │
│ MRK     $104.55     -1.17%   12,003,400  −0.05    below $100.00  ☐       × │
│ COST    $712.30     +0.34%    2,019,400  +0.18    —                          × │
└────────────────────────────────────────────────────────────────────────────┘
```

### Acceptance criteria

1. Symbol search is a free-text input validated against `^[A-Z][A-Z0-9.\-]{0,9}$` (uppercased automatically).
2. Each row stores: symbol, optional `(target_price, target_direction)` pair (`above` | `below`), `alert_enabled` flag, `last_alerted_at`, `last_triggered_price`, optional note.
3. **Adding from the Dashboard** uses HTMX so the page doesn't scroll/reload — the `+ Watch` button is replaced in-place with a green `✓ watching` pill. Symbols already on the watchlist render the green pill on Dashboard load (no re-add).
4. **Removing** is a one-click `×` (no confirm popup) — the row vanishes immediately.
5. **Target editor** lets the user type a price + select direction. Editing the target re-arms the alert.
6. **Alert checkbox** toggles `alert_enabled`. Hidden when no target is set. Auto-submits on change.
7. **Alert firing**: during the nightly job, after the scan, the watchlist alert checker runs. For each item where `alert_enabled = TRUE` and the latest close has crossed the target, a high-priority notification fires (subject: `AAPL crossed above $200.00`). Then `alert_enabled` flips to `FALSE` to prevent daily re-spam — the user can re-arm via the checkbox.
8. **Idempotent re-add**: `add_to_watchlist("AAPL")` for an existing symbol is a no-op (UPSERT on `symbol`); doesn't disrupt existing target/alert config.
9. **Tech score column** shows direction-agnostic technical bias for each symbol (Story 12 — works in this column without a firing strategy).

### Module mapping
- `stockscan.watchlist.store` (CRUD + last-bar enrichment)
- `stockscan.watchlist.alerts` (`check_and_fire_alerts()`, integrated into nightly job)
- `stockscan.web.routes.watchlist`
- `stockscan.notify` (existing notifier reused)

---

## Story 12 — Per-signal Technical Confirmation Score

> **As Thomas, I want each signal to come with a technical confirmation score that tells me whether RSI + MACD agree with the strategy's thesis, so I can quickly filter out signals where the technical setup is weak.**

### Why this is per-signal, not per-symbol

The same indicator value means different things to different strategies. RSI(14) at 30 *confirms* an RSI(2) mean-reversion entry (the pullback is real), but *contradicts* a Donchian breakout (momentum is fading). So the score has to know which strategy is firing.

### Trigger
- Computed automatically by the `ScanRunner` after persisting each signal (passing AND rejected — rejected ones get a score for diagnostic value).
- Displayed on the **Signals** page (new "Tech" column) and **Watchlist** page (neutral-mode score for symbols without a firing strategy).

### Happy path (UI)

```
Symbol  Strategy        Score  Tech    Entry     Stop      Qty   Date
──────  ──────────────  ─────  ──────  ────────  ────────  ───   ──────────
AAPL    rsi2_meanrev    0.92   +0.68 ▰ $189.40   $181.20    54   2026-04-27
MRK     rsi2_meanrev    0.81   −0.15 ▱ $104.55   $100.10   168   2026-04-27
NVDA    donchian_trend  0.74   +0.55 ▰ $892.10   $850.20    12   2026-04-27
```

Green `+0.68` = technicals confirm; red `−0.15` = technicals contradict.

### Acceptance criteria

1. **Composite score in `[-1, +1]`** — equal-weight average across registered indicators that produced a value. Indicators with insufficient history abstain (skipped, not zero-weighted).
2. **Strategy-aware via tags**, not strategy names. RSI scores low = +confirming for `mean_reversion`-tagged strategies; high = +confirming for `trend_following` / `breakout`. Adding a new strategy of an existing kind requires no indicator code changes.
3. **Neutral mode (`strategy=None`)** for the Watchlist: high RSI + positive/rising MACD = positive bullish bias; low RSI + negative/falling MACD = negative.
4. **Persisted** in `technical_scores` keyed `(symbol, as_of_date, strategy_name)`. Watchlist-mode scores use `strategy_name = '_neutral'`. Idempotent upsert.
5. **Backfillable** via `stockscan technical backfill` for past signals; `stockscan technical recompute` overwrites after scoring-formula changes.
6. **Plugin architecture** — new technical indicators are single-file drops into `stockscan/technical/indicators/`, auto-registered via `__init_subclass__`.
7. **Initial indicators**: RSI(14) and MACD(12,26,9). Each declares `values()` returning raw indicator output and `score(values, strategy)` returning the [-1, +1] confirmation.
8. **Sample-size guardrails**: when an indicator has insufficient bars (e.g., RSI needs ≥19, MACD needs ≥40 days for both readings), it returns `None` and the composite skips it. If every indicator abstains, the signal has no tech score (rendered as `—`).

### Module mapping
- `stockscan.technical.indicators` (TechnicalIndicator ABC + RSI + MACD)
- `stockscan.technical.score` (composite orchestration)
- `stockscan.technical.store` (`technical_scores` table)
- `stockscan.scan.runner` (calls `compute_technical_score` and `upsert_score` after each signal)
- `stockscan.web.routes.signals` (LEFT JOIN at query time)
- `stockscan.web.routes.watchlist` (computed on-render, neutral mode)

### Open question (deferred)
Per-signal breakdown UI on the signal detail page — show each indicator's raw values + sub-score so the operator can see *why* the composite landed where it did. Data is already persisted in `breakdown` JSONB; just needs a render. v1.5.

---

## Story 13 — Refresh fundamentals to enable market-cap-aware strategies

> **As Thomas, I want a single CLI command that pulls fundamentals (market cap, sector, P/E, etc.) for the S&P 500 universe, so strategies that filter by market cap or sector can run.**

### Trigger
- `stockscan refresh fundamentals` from the CLI.
- Auto-scheduled (weekly) is *not* set up by default — most fundamentals fields change quarterly with earnings, so a weekly cron is a reasonable interval.

### Happy path

```bash
$ uv run stockscan refresh fundamentals --current-only
→ refreshing fundamentals for 503 symbols (current S&P 500)
✓ 501 fetched · 2 missing · 0 failed
```

One EODHD `/fundamentals/{TICKER}.US` call per symbol. The full payload is hundreds of KB; we extract ~38 typed columns (market cap, sector, ratios, ...) and store the rest as `raw_payload` JSONB.

### Acceptance criteria

1. **One row per symbol** in `fundamentals_snapshot` (UPSERT on `symbol`). Latest-snapshot pattern, not point-in-time history.
2. **Typed columns** for the fields strategies will filter on at scan time: `market_cap`, `sector`, `industry`, `shares_outstanding`, `pe_ratio`, `forward_pe`, `eps_ttm`, `dividend_yield`, `beta`, `week_52_high`, `week_52_low`, plus ~25 more.
3. **`raw_payload` JSONB** retains the full provider response so future fields can be extracted without a migration.
4. **Indexes**: partial DESC index on `market_cap` (used by `market_cap_percentile` queries) and `sector`.
5. **`market_cap_percentile(symbol)` helper** uses Postgres `PERCENT_RANK()` over the snapshot table — used by the Largecap Rebound strategy's universe filter.
6. **Robust to provider quirks**: missing fields silently become `None`; the strategy abstains rather than incorrectly passing/failing on `None`.

### Module mapping
- `stockscan.data.providers.eodhd.get_fundamentals` (one API call per symbol)
- `stockscan.fundamentals.refresh.refresh_fundamentals` (loop + per-symbol upsert)
- `stockscan.fundamentals.store` (`upsert_fundamentals`, `market_cap_percentile`, `list_by_market_cap`)

### Caveat
Currently the snapshot is *latest only*, not historical. A backtest of 2015 applies *today's* market-cap percentiles to historical bars — minor look-ahead bias on the universe filter (prices are still historical and clean). True historical fundamentals (point-in-time per quarter) is a Phase 5 enhancement.

---

## Cross-cutting requirements

These apply to multiple stories.

### Determinism
Same inputs → same outputs. A signal generated twice for the same `(strategy, symbol, as_of_date, params_version)` is byte-identical. Required for trust and debugging.

### Observability
Every scan, signal, order, fill, and exit decision is persisted with full context (params, timestamps, inputs). The dashboard shows recent activity; the DB allows arbitrary post-hoc queries.

### Reversibility
No destructive operations from the UI without explicit confirmation. Notes are append-only by default (edit history kept). Closed trades can be re-opened only via a CLI admin command.

### Time discipline
All timestamps are `TIMESTAMPTZ` in UTC at the storage layer; the UI renders in America/New_York. The scheduler operates in America/New_York wall-clock to follow market hours.

### Responsive (mobile-functional in v1)

The dashboard must be **functional from a phone** in v1 — not pixel-polished, but every workflow reachable and usable on a ~390px-wide viewport (iPhone) and Android equivalents. Phone access is via existing WireGuard VPN to the home LAN; one-time `mkcert` root-CA install on the phone removes HTTPS warnings.

Layout principles:
- **Mobile-first Tailwind:** default styles target phone widths; `sm:` (≥640px), `md:` (≥768px), `lg:` (≥1024px) breakpoints scale up to desktop.
- **Tables collapse to cards** below `sm:`. Each scan-result row, position, or trade becomes a stacked card with the most important fields prominent.
- **Modals become full-screen pages** on mobile (the trade ticket is the main one). Back-button closes them. Overlays at phone size are awkward and steal scroll.
- **Navigation:** desktop sidebar collapses into a hamburger menu in the top bar on mobile.
- **Tap targets ≥44px square** (Apple HIG); generous spacing between interactive rows.
- **No hover-dependent UI.** All hover affordances duplicated as tap-to-reveal.
- **Charts (`lightweight-charts`) shrink to viewport width;** pinch-zoom and touch-pan work natively. Verify on iOS Safari and Android Chrome before Phase 2 sign-off.
- **Forms with many fields** stack vertically on mobile; numeric keyboards triggered for qty/price (`inputmode="decimal"`).
- **Markdown notes editor:** simple textarea + live preview pane that toggles on mobile rather than splits side-by-side.

Not required in v1: PWA install, offline mode, push notifications, dark mode (nice-to-have), landscape-specific layouts.

---

## Open Questions

1. **Cross-symbol base-rate search** (mentioned in Story 4): defer to v1.5 unless you want it now.
2. **Note templates configurable:** ship with built-in entry/exit templates, or let the user customize them? Default: ship built-in for v1, make configurable in v1.5.
3. **Notification quiet hours:** Discord alerts at 2am for a midnight reconciliation pass — silence outside trading hours? Probably yes.
