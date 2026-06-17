"""Tests for the MCP server layer (stockscan.mcp).

Unit tests here run without a database: they cover the JSON serializer, the
strategy tools (registry-only), write-gating, the HTTP-app construction, and an
in-memory MCP client round-trip. The DB-touching tools (signals, watchlist,
regime) are exercised under ``@pytest.mark.integration`` so they only run when a
postgres instance is available.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from decimal import Decimal
from enum import Enum

import pytest

from stockscan.mcp.serialize import jsonable
from stockscan.mcp.server import READ_TOOLS, WRITE_TOOLS, build_server
from stockscan.mcp.tools import scan as t_scan
from stockscan.mcp.tools import strategies as t_strategies


# ----------------------------------------------------------------------
# serialize.jsonable
# ----------------------------------------------------------------------
class _Side(Enum):
    LONG = "long"


@dataclasses.dataclass(frozen=True)
class _Row:
    sym: str
    price: Decimal
    when: dt.date


def test_jsonable_scalars_and_containers():
    assert jsonable(None) is None
    assert jsonable(3) == 3
    assert jsonable("x") == "x"
    assert jsonable(Decimal("1.50")) == 1.5
    assert jsonable(dt.date(2026, 6, 14)) == "2026-06-14"
    assert jsonable(dt.datetime(2026, 6, 14, 9, 30)) == "2026-06-14T09:30:00"
    assert jsonable(_Side.LONG) == "long"
    assert jsonable({"a", "b"}) == sorted(["a", "b"]) or set(jsonable({"a", "b"})) == {"a", "b"}


def test_jsonable_nested_dataclass_and_dict():
    row = _Row(sym="AAPL", price=Decimal("190.25"), when=dt.date(2026, 6, 1))
    out = jsonable({"row": row, "items": [row]})
    assert out["row"] == {"sym": "AAPL", "price": 190.25, "when": "2026-06-01"}
    assert out["items"][0]["price"] == 190.25


def test_jsonable_fallback_is_str():
    class Weird:
        def __repr__(self) -> str:
            return "weird-obj"

    assert jsonable(Weird()) == "weird-obj"


# ----------------------------------------------------------------------
# strategy tools (registry only, no DB)
# ----------------------------------------------------------------------
def test_list_strategies_returns_registered():
    out = t_strategies.list_strategies()
    names = {s["name"] for s in out["strategies"]}
    assert "rsi2_meanrev" in names
    for s in out["strategies"]:
        assert {"name", "version", "display_name", "tags"} <= set(s)


def test_get_strategy_ok_and_unknown():
    known = t_strategies.list_strategies()["strategies"][0]["name"]
    detail = t_strategies.get_strategy(known)
    assert detail["name"] == known
    assert "manual" in detail  # may be None, but key is present

    miss = t_strategies.get_strategy("nope_not_real")
    assert miss["error"] == "unknown_strategy"
    assert "known" in miss


# ----------------------------------------------------------------------
# write-gating
# ----------------------------------------------------------------------
async def test_write_tools_gated():
    ro = {t.name for t in await build_server(allow_writes=False, auth=None).list_tools()}
    rw = {t.name for t in await build_server(allow_writes=True, auth=None).list_tools()}

    read_names = {fn.__name__ for fn in READ_TOOLS}
    write_names = {fn.__name__ for fn in WRITE_TOOLS}

    assert read_names <= ro
    assert not (write_names & ro), "write tools must be absent when writes disabled"
    assert write_names <= rw, "write tools must be present when writes enabled"


# ----------------------------------------------------------------------
# refresh status (no DB when idle)
# ----------------------------------------------------------------------
def test_get_refresh_status_idle():
    from stockscan.scan.refresh_job import _reset_for_tests

    _reset_for_tests()
    out = t_scan.get_refresh_status()
    assert out["status"] == "idle"
    assert out["job"] is None


# ----------------------------------------------------------------------
# HTTP app construction (mount target)
# ----------------------------------------------------------------------
def test_http_app_builds_with_lifespan():
    server = build_server(allow_writes=False, auth=None)
    app = server.http_app(path="/", transport="http")
    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/" in paths
    assert callable(getattr(app, "lifespan", None))


# ----------------------------------------------------------------------
# in-memory MCP client round-trip (real protocol handshake)
# ----------------------------------------------------------------------
async def test_in_memory_client_handshake_and_call():
    from fastmcp import Client

    server = build_server(allow_writes=False, auth=None)
    async with Client(server) as client:
        tools = await client.list_tools()
        assert len(tools) == len(READ_TOOLS)
        result = await client.call_tool("list_strategies", {})
        data = result.structured_content
        assert data is not None
        assert any(s["name"] == "rsi2_meanrev" for s in data["strategies"])


# ----------------------------------------------------------------------
# DB-dependent tools — integration only
# ----------------------------------------------------------------------
def test_oauth_discovery_chain_and_dcr(monkeypatch):
    """The mounted OAuth discovery chain resolves and supports self-registration.

    Traces what a real MCP client does: hit /mcp -> read the 401's
    resource_metadata pointer -> fetch protected-resource metadata -> fetch the
    authorization-server metadata -> dynamically register a client. All of it
    must resolve where advertised (host root), not under the /mcp mount prefix.
    """
    import re
    from urllib.parse import urlparse

    from starlette.testclient import TestClient

    import stockscan.config as cfg
    from stockscan.web.app import create_app

    monkeypatch.setattr(cfg.settings, "mcp_enabled", True)
    monkeypatch.setattr(cfg.settings, "mcp_auth", "oauth")
    monkeypatch.setattr(cfg.settings, "mcp_base_url", "http://localhost:8000")
    monkeypatch.setattr(cfg.settings, "mcp_path", "/mcp")

    def path_of(u: str) -> str:
        return urlparse(u).path

    app = create_app()
    with TestClient(app) as c:
        r = c.get("/mcp", headers={"Accept": "application/json, text/event-stream"})
        assert r.status_code == 401
        prm_url = re.search(
            r'resource_metadata="([^"]+)"', r.headers.get("www-authenticate", "")
        ).group(1)

        prm = c.get(path_of(prm_url))
        assert prm.status_code == 200
        meta = prm.json()
        # protected-resource id resolves to <base>/mcp, not the doubled /mcp/mcp
        assert meta["resource"].rstrip("/").endswith("/mcp")
        assert "/mcp/mcp" not in meta["resource"]

        as_issuer = meta["authorization_servers"][0]
        asm = c.get(path_of(as_issuer.rstrip("/") + "/.well-known/oauth-authorization-server"))
        assert asm.status_code == 200
        reg = asm.json().get("registration_endpoint")
        assert reg, "Dynamic Client Registration endpoint must be advertised"

        dcr = c.post(
            path_of(reg),
            json={
                "redirect_uris": ["http://localhost/callback"],
                "client_name": "test-client",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            },
        )
        assert dcr.status_code in (200, 201)
        assert dcr.json().get("client_id")

        # The MCP routes are grafted (not catch-all mounted), so unknown paths
        # must still reach FastAPI's 404 handler — not be swallowed by MCP.
        assert c.get("/definitely-not-a-real-route").status_code == 404


def test_get_analysis_strips_chart_history():
    from stockscan.mcp.tools.analysis import _strip_chart_history

    d = {
        "symbol": "AAPL",
        "last_close": 296.42,
        "trend": {"bucket": "up"},
        "ohlc_history": [{"o": 1}, {"o": 2}],
        "closes_history": [[1, 2.0]],
        "volumes_history": [[1, 3.0]],
    }
    out = _strip_chart_history(d)
    for k in ("ohlc_history", "closes_history", "volumes_history"):
        assert k not in out
    # real analysis content is preserved
    assert out["symbol"] == "AAPL"
    assert out["trend"] == {"bucket": "up"}


def test_get_earnings_estimate_is_compact():
    from datetime import date as _date
    from types import SimpleNamespace

    from stockscan.mcp.tools.context import _ESTIMATE_KEYS, _compact_estimate

    rows = [
        SimpleNamespace(
            period_end=_date(2027, 10, 31), period="+1y", eps_estimate_avg=19.35,
            eps_growth=0.66, rev_estimate_avg=1.7e11, rev_growth=0.62,
            eps_analyst_count=44, eps_revisions_up_30d=36, eps_revisions_down_30d=1,
            junk="should not appear",
        ),
        SimpleNamespace(
            period_end=_date(2026, 9, 4), period="0q", eps_estimate_avg=1.7,
            eps_growth=0.2, rev_estimate_avg=1.6e10, rev_growth=0.18,
            eps_analyst_count=40, eps_revisions_up_30d=10, eps_revisions_down_30d=2,
            junk="should not appear",
        ),
    ]
    est = _compact_estimate(rows)
    # nearest forward period is picked, scalars only, junk dropped
    assert est["period_end"] == "2026-09-04"
    assert est["period"] == "0q"
    assert set(est.keys()) == set(_ESTIMATE_KEYS)
    assert "junk" not in est
    assert _compact_estimate([]) is None


def test_analyze_watchlist_unknown_facet():
    from stockscan.mcp.tools import analysis as t_analysis

    out = t_analysis.analyze_watchlist(facet="bogus")
    assert out["error"] == "unknown_facet"
    assert "summary" in out["valid"]


def test_options_summary_facet_is_lean():
    from types import SimpleNamespace

    from stockscan.mcp.tools.analysis import _project

    call = SimpleNamespace(strike=110.0, pct_otm=10.04, vol_pct=85.6, confluences=("res $109",))
    put = SimpleNamespace(strike=90.0, pct_otm=-9.96, vol_pct=85.6, confluences=("ema", "sup"))
    sset = SimpleNamespace(days_to_expiry=6, expiry_date=None, call=call, put=put)
    oc = SimpleNamespace(
        available=True,
        strike_sets=[sset],
        days_to_earnings=12,
        earnings_warning=False,
        pct_to_support=8.13,
        pct_to_resistance=0.94,
    )
    a = SimpleNamespace(symbol="TEST", available=True, last_close=100.0, options_context=oc)

    out = _project(a, "options_summary")
    assert out["symbol"] == "TEST"
    assert out["iv_pct"] == 86
    assert out["call_15d"] == {"strike": 110.0, "pct_otm": 10.0}
    assert out["put_15d"]["pct_otm"] == -10.0
    assert out["confluence_count"] == 3
    assert out["days_to_earnings"] == 12
    # Lean: no greeks and no confluence-string arrays in the payload.
    import json

    blob = json.dumps(out)
    assert "theta" not in blob and "vega" not in blob and "confluences" not in blob


def test_backfill_bars_requires_symbols():
    from stockscan.mcp.tools import data as t_data

    assert t_data._parse_symbols("AAPL, msft;NVDA") == ["AAPL", "MSFT", "NVDA"]
    assert t_data.backfill_bars("")["error"] == "no_symbols"
    # bad start date is caught before any provider/DB work
    assert t_data.backfill_bars("AAPL", start="not-a-date")["error"] == "invalid_start"


def test_refresh_tools_report_missing_api_key(monkeypatch):
    from pydantic import SecretStr

    import stockscan.config as cfg
    from stockscan.mcp.tools import data as t_data

    monkeypatch.setattr(cfg.settings, "eodhd_api_key", SecretStr(""))
    assert t_data.refresh_universe()["error"] == "no_api_key"


def test_new_tools_are_write_gated():
    read_names = {fn.__name__ for fn in READ_TOOLS}
    write_names = {fn.__name__ for fn in WRITE_TOOLS}
    # New reads present, new writes gated out of the read set.
    assert {"analyze_watchlist", "screen_by_market_cap", "list_backtests"} <= read_names
    assert {"add_symbols", "backfill_bars", "refresh_universe"} <= write_names
    assert not (read_names & write_names)


@pytest.mark.integration
def test_list_signals_shape():
    from stockscan.mcp.tools import signals as t_signals

    out = t_signals.list_signals(days=30)
    assert {"count", "passing", "rejected"} <= set(out)


@pytest.mark.integration
def test_list_watchlist_shape():
    from stockscan.mcp.tools import watchlist as t_watchlist

    out = t_watchlist.list_watchlist()
    assert {"count", "items"} <= set(out)
