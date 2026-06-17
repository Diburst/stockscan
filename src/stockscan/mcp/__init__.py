"""MCP server — exposes stockscan to LLM agents over the Model Context Protocol.

This package is a *thin adapter*: every tool calls the same service functions
the CLI and web UI already call (``stockscan.signals``, ``stockscan.watchlist``,
``stockscan.scan``, ``stockscan.analysis``, ``stockscan.regime``). No business
logic lives here — only argument shaping, write-gating, and JSON serialization.
It is the third caller of the single source of truth, alongside the CLI and the
web routes.

Nothing in this package is imported unless the MCP server is explicitly enabled
(``STOCKSCAN_MCP_ENABLED=true``) or the ``stockscan mcp`` CLI is invoked, so
``fastmcp`` stays an optional dependency for the core app.
"""

from __future__ import annotations
