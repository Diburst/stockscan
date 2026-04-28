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
    assert "Add a symbol" in r.text


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
