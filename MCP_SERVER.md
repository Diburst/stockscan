# stockscan MCP server

Exposes stockscan to LLM agents (Claude Desktop/Cowork/Code and any MCP-aware
client) over the Model Context Protocol. The server is a **thin adapter**: every
tool calls the same service functions the CLI and web UI already call
(`stockscan.signals`, `stockscan.watchlist`, `stockscan.scan`,
`stockscan.analysis`, `stockscan.regime`). It is the third caller of the single
source of truth — no business logic lives in `stockscan.mcp`.

## Tools

Read tools (always available) — 21:

- Signals: `list_signals` (filters: strategy, days, side, score band, symbol; gated to current strategy versions), `get_signal` (one by id, with the full score breakdown).
- Strategies: `list_strategies`, `get_strategy` (includes the long-form `manual`).
- Watchlist: `list_watchlists` (the named lists + counts), `list_watchlist` (items, enriched with latest close / % change).
- Analysis: `get_analysis` (full per-symbol pipeline), `analyze_watchlist` (cross-section across watched symbols, with a `facet` to keep payloads small — summary/trend/volatility/momentum/levels/`options_summary` (lean: IV + nearest 15Δ strikes + earnings flag + confluence count)/`options_context` (full strike sets + greeks — large)/full), `get_regime`.
- Market context: `get_fundamentals`, `screen_by_market_cap`, `get_earnings`, `upcoming_earnings`, `get_news` (headlines/snippets), `get_article` (full body on demand — re-fetches from EODHD, ~1 credit, needs key), `get_insider` (per-symbol), `watchlist_insider` (net-buys across the watchlist), `upcoming_econ_events`.
- Backtests: `list_backtests`, `get_backtest` (export a run's trades/score-breakdowns/equity).
- `get_refresh_status` — read side of the fire-and-poll refresh.

Write tools (only when writes are enabled — see `STOCKSCAN_MCP_ALLOW_WRITES`) — 16:

- Watchlist management: `add_to_watchlist`, `add_symbols` (bulk), `remove_from_watchlist`, `create_watchlist`, `rename_watchlist`, `delete_watchlist`, `set_target`, `toggle_alert`.
- Scans: `run_scan` (run a strategy or all, persist signals), `refresh_data` (background bars+strategies refresh — **fire-and-poll**: returns immediately, poll `get_refresh_status`).
- Data backfill / refresh (external EODHD API, costs credits; return `{"error": "no_api_key"}` if unconfigured): `backfill_bars`, `refresh_fundamentals`, `refresh_news`, `refresh_earnings`, `refresh_insider` (~23h cooldown), `refresh_universe`.

Deliberately out of scope: anything that places trades, deletes signals, trains
ML models, or runs migrations, plus live `run_backtest` (minutes of CPU). Those
stay CLI/human-only.

The cross-section tool (`analyze_watchlist`) runs the full per-symbol pipeline
for every watched symbol, so it can be slow on large lists; it returns only the
requested `facet` per symbol by default to keep the response small.

`stockscan mcp tools [--allow-writes]` prints the live tool list.

## Configuration (env / `.env`)

| Var | Default | Meaning |
|---|---|---|
| `STOCKSCAN_MCP_ENABLED` | `false` | Mount the MCP server on the web app. When false, `fastmcp` is never imported. |
| `STOCKSCAN_MCP_ALLOW_WRITES` | `false` | Expose the mutating/expensive tools. |
| `STOCKSCAN_MCP_AUTH` | `oauth` | `oauth` (self-contained OAuth 2.1) or `none` (local stdio dev only). |
| `STOCKSCAN_MCP_BASE_URL` | `http://127.0.0.1:8000` | Externally reachable base URL (e.g. your Tailscale hostname) — the OAuth issuer/resource base. |
| `STOCKSCAN_MCP_PATH` | `/mcp` | Mount path for the MCP endpoint. |

## Running it

**Local, same machine (no Tailscale, no HTTPS, no auth)** — the simplest way to
try it on one computer. Serves the web UI + MCP over plain HTTP on loopback:

```bash
uv sync --all-extras        # one-time: installs the mcp extra (fastmcp)
make run-mcp-local          # http://localhost:8000  (MCP at /mcp)
```

Binds to `127.0.0.1` only, so the unauthenticated endpoint isn't reachable from
your LAN. Connect a client on the same machine to `http://localhost:8000/mcp`.
`localhost` is exempt from the OAuth-issuer HTTPS requirement, so no certs are
needed. Add `STOCKSCAN_MCP_ALLOW_WRITES=true` to expose write tools.

**Local dev (stdio, no auth)** — alternative for same-machine use; needs no URL
or auth at all (point the client at the command):

```bash
stockscan mcp serve --transport stdio [--allow-writes]
```

**Remote, composed with the web app (primary deployment).** The MCP *message*
endpoint is served at `/mcp`, alongside the UI, in one process. The MCP app's
routes are grafted onto the FastAPI router so its OAuth and `/.well-known/*`
routes land at the host **root** — where MCP clients look for discovery (see
"Auth model"). Web routers are registered first, so the UI always wins for its
own paths, and unknown paths still hit the friendly 404.

The one-command path is `make run-web`, which exposes the app over HTTPS on your
tailnet and prints the connector URL:

```bash
make run-web                              # reads-only tools
STOCKSCAN_MCP_ALLOW_WRITES=true make run-web   # also expose write tools
```

`make run-web` binds uvicorn to `127.0.0.1:8000`, derives your `*.ts.net`
hostname automatically, sets `STOCKSCAN_MCP_ENABLED=true` and
`STOCKSCAN_MCP_BASE_URL` for you, and runs Tailscale Serve to terminate TLS. It
prints `https://<machine>.<tailnet>.ts.net/mcp` — the URL to paste into Claude.
(`make run-web-local` is the plain dev server with no MCP/tailscale.)

The equivalent by hand:

```bash
tailscale serve --bg http://127.0.0.1:8000   # HTTPS:443 -> local port, valid *.ts.net cert
STOCKSCAN_MCP_ENABLED=true \
STOCKSCAN_MCP_BASE_URL=https://<machine>.<tailnet>.ts.net \
  uvicorn stockscan.web.app:app --host 127.0.0.1 --port 8000
```

`STOCKSCAN_MCP_BASE_URL` must be the externally reachable URL and, for anything
other than `localhost`, it **must be HTTPS** — the OAuth 2.1 issuer URL is
required to be HTTPS, which is why TLS termination via Tailscale Serve is needed.
Never expose this open on the public internet — keep it on your tailnet.

**Standalone HTTP** (MCP only, no web UI) is also available; it serves MCP at the
root path of its own port:

```bash
stockscan mcp serve --transport http --host 127.0.0.1 --port 8000 [--allow-writes]
```

## Private deployment: Mac Mini + Tailscale + Claude mobile

This is the recommended setup: the app stays off the public internet entirely
and is reachable only by your own devices over your tailnet. The in-memory OAuth
provider is safe here precisely because nothing but your devices can reach the
URL. (You do NOT need a public reverse proxy for this — Tailscale provides both
the reachability and a valid `*.ts.net` HTTPS certificate.)

One-time setup:

1. **Install Tailscale on the Mac Mini** (`brew install tailscale` or the App
   Store app), then `tailscale up` and sign in to your tailnet.
2. **Enable HTTPS certificates** for your tailnet once, in the Tailscale admin
   console (DNS → enable MagicDNS, and enable HTTPS Certificates). This lets
   Tailscale Serve obtain a real cert for `<machine>.<tailnet>.ts.net`.
3. **Install Tailscale on your phone** (iOS/Android app) and sign in to the same
   tailnet. Keep it connected — that's what lets the Claude mobile app reach the
   Mini.
4. On the Mini, install the app's deps including the MCP extra:
   `uv sync --all-extras`.

Run it (from the repo on the Mini):

```bash
make run-web        # or: STOCKSCAN_MCP_ALLOW_WRITES=true make run-web
```

`make run-web` derives your `<machine>.<tailnet>.ts.net` hostname, runs Tailscale
Serve for HTTPS, and prints the connector URL. For an always-on host you'll want
this to survive logout/reboot — run it under `tmux`/`screen`, or wrap it in a
`launchd` user agent (ask and I'll generate the plist). Note `--reload` in
`make run-web` is a dev convenience; drop it for a stable long-running host.

Connect the **Claude mobile app**: Settings → Connectors → Add custom connector →
URL `https://<machine>.<tailnet>.ts.net/mcp` → Connect → approve. (With your
phone on the tailnet, this resolves privately; the cert is valid, so no warnings.)

## Connect Claude Desktop / Cowork

1. Start the server (the "primary deployment" command above) and confirm the UI
   loads at your tailnet URL.
2. In the Claude desktop app: **Settings → Connectors → Add custom connector**.
3. Name it `stockscan` and set the URL to `https://<machine>.<tailnet>.ts.net/mcp`.
4. Save and click **Connect**. Claude opens a browser to the server's OAuth
   consent screen (the server self-registers your client via Dynamic Client
   Registration — no client id/secret to copy). Approve it.
5. The stockscan tools now appear in Claude. Try: *"List my recent reversal_swing
   signals"* or *"What's the current market regime?"*.

If the connector errors on connect, it's almost always the discovery chain not
resolving over HTTPS — confirm `https://<machine>.<tailnet>.ts.net/.well-known/oauth-protected-resource/mcp`
returns JSON in a browser, and that `STOCKSCAN_MCP_BASE_URL` exactly matches the
HTTPS host you're connecting to.

## Auth model

`STOCKSCAN_MCP_AUTH=oauth` uses FastMCP's `InMemoryOAuthProvider` — a
self-contained OAuth 2.1 authorization server with Dynamic Client Registration.
For a single-user Tailscale deployment this is the right fit: the MCP client
self-registers and completes the OAuth flow itself, no external identity
provider and no pre-shared client id. Registered clients/tokens live in memory,
so they re-register after a server restart (fine for personal use). If you later
expose this beyond your tailnet, switch to a hosted identity provider (FastMCP
ships Google/GitHub/Auth0/WorkOS/etc. providers) by swapping `build_auth()` in
`stockscan/mcp/server.py`.

Discovery is wired so the advertised URLs match where the routes physically
live. The MCP routes are grafted onto the FastAPI router: the AS metadata, `/authorize`,
`/token`, `/register`, and `/.well-known/oauth-protected-resource/mcp` all sit at
the host root (where the provider advertises them), while only the message
endpoint is at `/mcp`. The protected-resource id resolves to `<base>/mcp`.

## What's verified vs. what needs a real client

Verified in-process (unit + integration tests, `tests/test_mcp_tools.py`):

- Tool registration and write-gating (reads always; writes only when enabled).
- A real MCP protocol handshake (in-memory client) and tool calls.
- Composition with the FastAPI app, and the chained lifespan correctly starting
  the StreamableHTTP session manager (the "nested lifespan" gotcha).
- **The full OAuth discovery chain**: `/mcp` → 401 with a resource-metadata
  pointer → protected-resource metadata (200, resource = `<base>/mcp`) →
  authorization-server metadata (200) → **Dynamic Client Registration** (POST
  `/register` → 201 with a client_id). The web UI still owns its own paths.

**Needs a one-time manual check** (requires a real browser + your HTTPS tailnet
URL, so it can't be automated here): the *interactive* part of the flow — the
authorization-code redirect, the consent screen, and token issuance — plus
confirming TLS is terminated correctly by Tailscale Serve. Add the connector in
Claude (steps above) and approve once; that exercises exactly this remaining
piece.

## Architecture notes

- `src/stockscan/signals.py` — the signals query service (`query_signals`,
  `get_signal`), lifted out of `web/routes/signals.py` so the web list, the
  refresh re-render, and the MCP `list_signals` tool share one SQL definition.
- `src/stockscan/mcp/tools/*.py` — plain functions (no `fastmcp` import), so
  they're unit-testable directly.
- `src/stockscan/mcp/server.py` — assembles the FastMCP instance, gates writes,
  wires auth, and builds the HTTP app.
- `src/stockscan/web/app.py` — grafts the MCP app's routes onto the FastAPI
  router (message endpoint at `/mcp`, OAuth + well-known at root) after the web
  routers, and chains its lifespan, when `STOCKSCAN_MCP_ENABLED` is set. Routes
  are grafted rather than mounted as a catch-all so unknown paths still hit the
  friendly 404. The disabled path is unchanged.
