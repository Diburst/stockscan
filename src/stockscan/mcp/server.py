"""Assemble the FastMCP server: register tools, gate writes, wire auth.

The tool functions live in ``stockscan.mcp.tools`` as plain functions; this
module decides which ones are exposed (writes only when enabled) and how the
server is transported (stdio for local dev, streamable-HTTP mounted on the
FastAPI app for remote use). ``fastmcp`` is imported lazily so the core app
never hard-depends on it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from stockscan import __version__
from stockscan.config import settings
from stockscan.mcp.tools import analysis as t_analysis
from stockscan.mcp.tools import backtests as t_backtests
from stockscan.mcp.tools import context as t_context
from stockscan.mcp.tools import data as t_data
from stockscan.mcp.tools import proposals as t_proposals
from stockscan.mcp.tools import scan as t_scan
from stockscan.mcp.tools import signals as t_signals
from stockscan.mcp.tools import strategies as t_strategies
from stockscan.mcp.tools import watchlist as t_watchlist

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fastmcp import FastMCP

log = logging.getLogger(__name__)

INSTRUCTIONS = (
    "stockscan is a personal swing-trading scanner, backtester, and position "
    "manager for US equities. Use these tools to read trading signals and their "
    "score breakdowns, inspect strategies, read per-symbol technical analysis "
    "and the market regime, and manage the watchlist. Signals are gated to each "
    "strategy's current version. Write tools (watchlist edits, running scans, "
    "refreshing data) are only available when the server is started with writes "
    "enabled; refresh_data is fire-and-poll — start it, then poll "
    "get_refresh_status."
)

# Read-only tools: always registered.
READ_TOOLS = (
    # signals
    t_signals.list_signals,
    t_signals.get_signal,
    # strategies
    t_strategies.list_strategies,
    t_strategies.get_strategy,
    # watchlist
    t_watchlist.list_watchlists,
    t_watchlist.list_watchlist,
    # analysis + regime
    t_analysis.get_analysis,
    t_analysis.analyze_watchlist,
    t_analysis.get_regime,
    # options-premium proposals
    t_proposals.propose_options,
    # market context
    t_context.get_fundamentals,
    t_context.screen_by_market_cap,
    t_context.get_earnings,
    t_context.upcoming_earnings,
    t_context.get_news,
    t_context.get_article,
    t_context.get_insider,
    t_context.watchlist_insider,
    t_context.upcoming_econ_events,
    # backtests
    t_backtests.list_backtests,
    t_backtests.get_backtest,
    # refresh status (read side of the fire-and-poll refresh)
    t_scan.get_refresh_status,
)

# Mutating / expensive tools: registered only when writes are enabled.
WRITE_TOOLS = (
    # watchlist management
    t_watchlist.add_to_watchlist,
    t_watchlist.add_symbols,
    t_watchlist.remove_from_watchlist,
    t_watchlist.create_watchlist,
    t_watchlist.rename_watchlist,
    t_watchlist.delete_watchlist,
    t_watchlist.set_target,
    t_watchlist.toggle_alert,
    # scans + refresh
    t_scan.run_scan,
    t_scan.refresh_data,
    # data backfill / refresh (external API, credits)
    t_data.backfill_bars,
    t_data.refresh_fundamentals,
    t_data.refresh_news,
    t_data.refresh_earnings,
    t_data.refresh_insider,
    t_data.refresh_universe,
)


def build_auth() -> Any | None:
    """Build the auth provider from settings.

    ``STOCKSCAN_MCP_AUTH=oauth`` (default for remote use) returns a
    self-contained OAuth 2.1 authorization server (FastMCP's
    ``InMemoryOAuthProvider``) with dynamic client registration — the right fit
    for a single-user Tailscale deployment, where the MCP client performs the
    OAuth handshake automatically and no external identity provider is needed.
    ``STOCKSCAN_MCP_AUTH=none`` disables auth (use only for local stdio dev).
    """
    if settings.mcp_auth == "none":
        return None
    if settings.mcp_auth == "oauth":
        from fastmcp.server.auth.providers.in_memory import InMemoryOAuthProvider
        from mcp.server.auth.settings import ClientRegistrationOptions

        # base_url is the AS issuer (served at the host root: /authorize, /token,
        # /.well-known/oauth-authorization-server). resource_base_url is also the
        # root — FastMCP appends the MCP path itself, so the protected-resource
        # id resolves to <base>/mcp (NOT <base>/mcp/mcp). Dynamic Client
        # Registration is enabled so MCP clients (e.g. Claude Desktop) can
        # self-register without a pre-shared client id.
        base = settings.mcp_base_url.rstrip("/")
        return InMemoryOAuthProvider(
            base_url=base,
            resource_base_url=base,
            client_registration_options=ClientRegistrationOptions(enabled=True),
        )
    raise ValueError(f"Unknown STOCKSCAN_MCP_AUTH value: {settings.mcp_auth!r}")


def build_server(*, allow_writes: bool | None = None, auth: Any | None = None) -> FastMCP:
    """Create the FastMCP server with tools registered.

    Args:
        allow_writes: Override ``settings.mcp_allow_writes``. When False, only
            read tools are exposed.
        auth: Auth provider; defaults to None (caller passes one for HTTP).

    Returns:
        A configured ``fastmcp.FastMCP`` instance.
    """
    from fastmcp import FastMCP

    allow = settings.mcp_allow_writes if allow_writes is None else allow_writes
    mcp: FastMCP = FastMCP(
        name="stockscan",
        instructions=INSTRUCTIONS,
        version=__version__,
        auth=auth,
    )
    for fn in READ_TOOLS:
        mcp.tool(fn)
    if allow:
        for fn in WRITE_TOOLS:
            mcp.tool(fn)
    log.info(
        "MCP server built: %d read tools, %d write tools (writes=%s)",
        len(READ_TOOLS),
        len(WRITE_TOOLS) if allow else 0,
        allow,
    )
    return mcp


def build_mcp_http_app(*, transport: str = "http") -> Any:
    """Build the streamable-HTTP ASGI app to compose with the FastAPI server.

    The MCP *message* endpoint sits at ``settings.mcp_path`` (e.g. ``/mcp``)
    within the returned app; its OAuth routes (``/authorize``, ``/token``,
    ``/register``, ``/.well-known/...``) sit at the app root. ``create_app``
    grafts these concrete routes onto the FastAPI router (rather than mounting
    the whole app) so those OAuth/well-known paths land at the host root — where
    the auth provider advertises them and MCP clients look for discovery — while
    unknown paths still reach FastAPI's 404 handler. The web routers are
    registered first, so they always win for their own paths.

    The returned app carries its own lifespan, which the parent FastAPI app must
    run (FastAPI does not auto-detect a mounted app's lifespan). See
    ``stockscan.web.app.create_app``.
    """
    mcp = build_server(auth=build_auth())
    return mcp.http_app(path=settings.mcp_path or "/mcp", transport=transport)
