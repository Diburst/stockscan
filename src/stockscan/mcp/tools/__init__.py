"""MCP tool functions, grouped by noun.

Each function is a plain Python function (no ``fastmcp`` import) so it can be
unit-tested directly; ``stockscan.mcp.server`` registers them on the FastMCP
instance. Functions return JSON-safe dicts (via ``stockscan.mcp.serialize``).
Tools that mutate state or trigger expensive work live in the WRITE group in
``server.py`` and are only registered when writes are enabled.
"""

from __future__ import annotations
