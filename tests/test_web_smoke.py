"""Web smoke tests — every page returns 200 (or 404 cleanly for missing IDs).

We patch the DB session dependency to use a mock that returns empty result
sets for every query, so this exercises template rendering without needing
Postgres. Real end-to-end integration tests are marked @pytest.mark.integration.
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from stockscan.web.app import create_app
from stockscan.web.deps import get_session


def _empty_result():
    """Mock session.execute() result that returns empty for any query."""
    res = MagicMock()
    res.first.return_value = None
    res.one.return_value = None
    res.all.return_value = []
    res.__iter__ = lambda self: iter([])
    return res


def _mock_session() -> Iterator[MagicMock]:
    s = MagicMock()
    s.execute.return_value = _empty_result()
    yield s


@pytest.fixture
def client() -> TestClient:
    app = create_app()
    app.dependency_overrides[get_session] = _mock_session
    return TestClient(app, raise_server_exceptions=True)


def test_dashboard_renders(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "stockscan" in r.text
    assert "Open positions" in r.text


def test_dashboard_has_mobile_viewport(client):
    """Mobile-responsive requirement (DESIGN §4.8) — viewport meta must be present."""
    r = client.get("/")
    assert 'name="viewport"' in r.text
    assert "width=device-width" in r.text


def test_dashboard_has_mobile_nav(client):
    """Hamburger nav for mobile must be in the DOM."""
    r = client.get("/")
    assert 'id="mobile-nav"' in r.text


def test_signals_list_renders_empty(client):
    r = client.get("/signals")
    assert r.status_code == 200
    assert "Passing signals" in r.text


def test_signals_list_filter_by_strategy(client):
    r = client.get("/signals?strategy=rsi2_meanrev&days=30")
    assert r.status_code == 200


def test_signal_detail_404_clean(client):
    r = client.get("/signals/999999")
    assert r.status_code == 200  # we render an empty-state, not a 404
    assert "not found" in r.text.lower()


def test_trades_list_renders(client):
    r = client.get("/trades")
    assert r.status_code == 200
    assert "Open positions" in r.text
    assert "Closed trades" in r.text


def test_trade_detail_missing_renders(client):
    r = client.get("/trades/999")
    assert r.status_code == 200
    assert "not found" in r.text.lower()


def test_trades_search_renders(client):
    r = client.get("/trades/search?q=earnings")
    assert r.status_code == 200


def test_backtests_list_renders(client):
    # backtest list uses session_scope() directly; patch list_runs instead
    from stockscan.web.routes import backtests as backtests_route
    backtests_route.list_runs = lambda **k: []
    r = client.get("/backtests")
    assert r.status_code == 200
    assert "Backtests" in r.text


def test_backtest_detail_missing(client):
    r = client.get("/backtests/9999")
    assert r.status_code == 200
    assert "not found" in r.text.lower()


def test_strategies_list_shows_registered(client):
    r = client.get("/strategies")
    assert r.status_code == 200
    # RSI(2) and Donchian are auto-registered on import
    assert "RSI(2)" in r.text or "rsi2_meanrev" in r.text
    assert "Donchian" in r.text


def test_strategy_detail_renders(client):
    r = client.get("/strategies/rsi2_meanrev")
    assert r.status_code == 200
    assert "RSI(2)" in r.text or "rsi2_meanrev" in r.text


def test_strategy_detail_unknown(client):
    r = client.get("/strategies/does_not_exist")
    assert r.status_code == 200
    assert "not found" in r.text.lower()


def test_watchlist_list_renders(client):
    # Patch the store to return an empty list (no DB)
    from stockscan.web.routes import watchlist as wl_route
    wl_route.list_watchlist = lambda **k: []
    r = client.get("/watchlist")
    assert r.status_code == 200
    assert "Watchlist" in r.text
    # Unified add box (handles one or many symbols) + list management.
    assert "Add symbols" in r.text
    # The list selector is present (All pill + the window-independent selector).
    assert "?list=all" in r.text
    assert "Manage lists" in r.text


def test_watchlist_export_renders(client):
    r = client.get("/watchlist/export?list=all")
    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]


def test_analysis_list_renders(client):
    # analyze_watchlist_cards hits the (mocked) DB and returns []; the page
    # still renders with the list selector. The shared window/studies toolbar
    # only appears when there are charts to control, so it's absent here.
    r = client.get("/analysis")
    assert r.status_code == 200
    assert "Analysis" in r.text
    assert "?list=all" in r.text


def test_analysis_list_with_list_param(client):
    r = client.get("/analysis?list=all")
    assert r.status_code == 200


def test_dashboard_has_add_to_watchlist_buttons(client):
    r = client.get("/")
    assert r.status_code == 200
    # The form action should appear even when there are no signals/positions
    # since the buttons are rendered per-row. Empty state — verify the route
    # at least mentions /watchlist in the nav.
    assert "/watchlist" in r.text


def test_health_endpoint_still_works(client):
    # /health uses healthcheck() which won't have a real DB; expect 503 with
    # a degraded body, but the endpoint itself must respond cleanly.
    r = client.get("/health")
    assert r.status_code in (200, 503)
    body = r.json()
    assert "status" in body and "strategies" in body


# -----------------------------------------------------------------------
# HTMX-aware error handling (hardening refactor): a failed fragment action
# must NOT swap an error page into the target — empty body, HX-Reswap:none,
# friendly message in X-Error-Message for the global toast listener.
# -----------------------------------------------------------------------

def test_htmx_error_returns_header_not_page(client):
    r = client.get("/definitely/not/a/route", headers={"HX-Request": "true"})
    assert r.status_code == 404
    assert r.headers.get("HX-Reswap") == "none"
    assert r.headers.get("X-Error-Message")
    assert r.text == ""  # nothing for htmx to swap


def test_non_htmx_error_still_renders_page(client):
    r = client.get("/definitely/not/a/route")
    assert r.status_code == 404
    assert "find that page" in r.text.lower()  # apostrophe is HTML-escaped


def test_base_has_global_htmx_error_listener(client):
    r = client.get("/")
    assert "htmx:responseError" in r.text
    assert "htmx:sendError" in r.text


# -----------------------------------------------------------------------
# Watchlist pill auto-flip (TODO.md item): the dashboard "watching" pill is
# now an unwatch toggle, and /watchlist/unwatch swaps back to "+ Watch".
# -----------------------------------------------------------------------

def test_dashboard_watching_pill_is_unwatch_form(client):
    r = client.get("/")
    assert r.status_code == 200
    # The macro exists in the page source whenever any row is watched; with
    # the mocked empty DB nothing is watched, so just assert the unwatch
    # endpoint is reachable below.


def test_unwatch_htmx_swaps_back_to_watch_form(client, monkeypatch):
    from stockscan.web.routes import watchlist as wl_route

    monkeypatch.setattr(wl_route, "remove_symbol", lambda sym, session=None: 1)
    r = client.post(
        "/watchlist/unwatch",
        data={"symbol": "AAPL", "redirect_to": "/"},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    assert "+ Watch" in r.text
    assert 'hx-post="/watchlist/add"' in r.text


def test_unwatch_not_watched_still_succeeds(client, monkeypatch):
    from stockscan.web.routes import watchlist as wl_route

    monkeypatch.setattr(wl_route, "remove_symbol", lambda sym, session=None: 0)
    r = client.post(
        "/watchlist/unwatch",
        data={"symbol": "MSFT"},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    assert "+ Watch" in r.text
