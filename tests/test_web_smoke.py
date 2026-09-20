"""Web smoke tests — every page returns 200 (or 404 cleanly for missing IDs).

We patch the DB session dependency to use a mock that returns empty result
sets for every query, so this exercises template rendering without needing
Postgres. Real end-to-end integration tests are marked @pytest.mark.integration.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from stockscan.regime import MarketRegime, regime_label
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


def _nav_labels(html: str) -> list[str]:
    """Top-nav link labels, in order, from the desktop <nav>."""
    import re

    nav = html.split('<nav class="hidden sm:flex', 1)[1].split("</nav>", 1)[0]
    return [m.strip() for m in re.findall(r'<a href="[^"]+"[^>]*>\s*([^<]+?)\s*</a>', nav)]


def test_nav_is_seven_pages_with_reference_links_in_footer(client):
    r = client.get("/")
    assert _nav_labels(r.text) == [
        "Dashboard", "Signals", "Watchlist", "Options", "Hedge", "Trades", "Backtests",
    ]
    # Analysis is reached from symbol links, not the nav; Strategies + Docs
    # sit in the footer on every page.
    footer = r.text.split("<footer", 1)[1]
    assert 'href="/strategies"' in footer and 'href="/docs"' in footer
    assert 'href="/analysis"' not in r.text.split("<footer", 1)[0].split("</header>", 1)[0]
    # The mobile nav mirrors the desktop one.
    mobile = r.text.split('id="mobile-nav"', 1)[1].split("</nav>", 1)[0]
    assert 'href="/backtests"' in mobile and 'href="/strategies"' not in mobile


def test_dashboard_latest_scan_card(client, monkeypatch):
    """The card shows only status='new' rows from the latest scan date."""
    from stockscan.web.routes import dashboard as dash_route

    passing = SimpleNamespace(as_of_date=date(2026, 9, 18), n=2)
    rows = [
        SimpleNamespace(signal_id=1, strategy_name="rsi2_meanrev", symbol="AAPL",
                        side="long", score=Decimal("0.04"), suggested_entry=Decimal("187.2"),
                        suggested_stop=None, suggested_qty=53, as_of_date=date(2026, 9, 18)),
        SimpleNamespace(signal_id=2, strategy_name="momentum_52w_high", symbol="NVDA",
                        side="long", score=Decimal("1.2"), suggested_entry=Decimal("120"),
                        suggested_stop=Decimal("102"), suggested_qty=10, as_of_date=date(2026, 9, 18)),
    ]
    app = create_app()
    session = MagicMock()

    def _execute(stmt, params=None):
        sql = str(stmt)
        res = _empty_result()
        if "count(*)" in sql:
            res.first.return_value = passing
        elif "s.as_of_date = :d" in sql:
            res.all.return_value = rows
        return res

    session.execute.side_effect = _execute

    def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    monkeypatch.setattr(dash_route, "list_open_trades", lambda session=None: [])
    monkeypatch.setattr(dash_route, "watchlist_symbols", lambda session=None: set())
    r = TestClient(app, raise_server_exceptions=True).get("/")
    assert r.status_code == 200
    assert "Latest scan · 2026-09-18" in r.text and "2 passing" in r.text
    assert 'href="/signals/1#paper-trade"' in r.text and 'href="/signals/2/base-rates"' in r.text
    assert "$102.00" in r.text
    assert 'href="/signals"' in r.text
    # The rows query pins the latest scan date, not a monthly window.
    sql = " ".join(str(c.args[0]) for c in session.execute.call_args_list)
    assert "s.as_of_date = :d" in sql and "s.status = 'new'" in sql


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
    # Both book strategies are auto-registered on import.
    assert "RSI(2)" in r.text
    assert "52-Week-High" in r.text


def test_strategy_detail_renders(client):
    r = client.get("/strategies/rsi2_meanrev")
    assert r.status_code == 200
    assert "RSI(2)" in r.text
    # Sizing summary + every knob off the class + the sector-composite input.
    assert "fixed 10% of equity per position" in r.text
    assert "does not apply" in r.text
    assert "rsi_entry" in r.text and "max_holding_bars" in r.text
    assert "$EWSECTOR" in r.text


def test_strategy_detail_stop_based_sizing(client):
    r = client.get("/strategies/momentum_52w_high")
    assert r.status_code == 200
    assert "risk 0.75% of equity against the stop" in r.text
    assert "Max open positions" in r.text and ">10<" in r.text
    assert "sizes down in the top tercile" in r.text


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


# -----------------------------------------------------------------------
# Regime layer surfaces: the dashboard card and the signal-detail sizing /
# regime-context cards, rendered against a real MarketRegime row.
# -----------------------------------------------------------------------

def _regime(*, gate_open=True, stress=False, vol_scalar="0.7200"):
    return MarketRegime(
        as_of_date=date(2026, 9, 18),
        regime=regime_label(trend_gate_open=gate_open, credit_stress_flag=stress),
        trend_gate_open=gate_open,
        days_on_side=7,
        spy_close=Decimal("512.30"),
        spy_sma200=Decimal("498.10"),
        spy_sma200_slope_20d=Decimal("0.012"),
        realized_vol_20d=Decimal("0.2222"),
        realized_vol_pct_rank=Decimal("0.91"),
        vol_scalar=Decimal(vol_scalar),
        hy_oas_level=Decimal("3.41"),
        hy_oas_pct_rank=Decimal("0.22"),
        credit_stress_flag=stress,
    )


def test_dashboard_regime_card_shows_controls(client, monkeypatch):
    from stockscan.web.routes import dashboard as dash_route

    monkeypatch.setattr(dash_route, "latest_regime", lambda session=None: _regime())
    r = client.get("/")
    assert r.status_code == 200
    assert "risk on" in r.text
    assert "7 closes on side" in r.text
    assert "22.2% realized" in r.text and "rank 91%" in r.text
    assert "×0.72" in r.text
    assert "HY OAS 341 bp" in r.text
    # Per-strategy sizing lines: momentum opts into the scalar, RSI(2) does not.
    assert "vol scalar applies" in r.text
    assert "vol scalar does not apply" in r.text
    assert "no new entries" not in r.text
    # Safari-safe: the controls are a table, never a styled <summary>.
    assert "<summary class=\"grid" not in r.text and "<summary class=\"flex" not in r.text
    assert "How to read this card" in r.text


def test_dashboard_regime_card_closed_gate(client, monkeypatch):
    from stockscan.web.routes import dashboard as dash_route

    monkeypatch.setattr(
        dash_route, "latest_regime", lambda session=None: _regime(gate_open=False)
    )
    r = client.get("/")
    assert r.status_code == 200
    assert "risk off" in r.text
    assert "no new longs" in r.text
    assert "no new entries" in r.text


def _signal_row(**overrides):
    base = dict(
        signal_id=42, strategy_name="rsi2_meanrev",
        strategy_version="2.0.0", symbol="AAPL", side="long",
        score=Decimal("0.0312"), status="new", as_of_date=date(2026, 9, 18),
        suggested_entry=Decimal("187.20"), suggested_stop=None,
        suggested_target=None, suggested_qty=53, rejected_reason=None,
        metadata={
            "rsi_2": 4.1, "sma_200": 171.5, "stock_return_1m": -0.061,
            "sector_return_1m": -0.012, "idiosyncratic_drop": 0.049,
            "relative_volume": 0.9,
        },
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _client_with_signal(signal, regime):
    app = create_app()

    def _session():
        s = MagicMock()
        res = MagicMock()
        res.first.return_value = signal
        res.all.return_value = []
        s.execute.return_value = res
        yield s

    app.dependency_overrides[get_session] = _session
    from stockscan.web.routes import signals as signals_route

    signals_route.get_regime = lambda as_of, session=None: regime
    return TestClient(app, raise_server_exceptions=True)


def test_signal_detail_stopless_strategy(monkeypatch):
    client = _client_with_signal(_signal_row(), _regime())
    r = client.get("/signals/42")
    assert r.status_code == 200
    # No stop: the Outcome card says so instead of inventing a $0 stop.
    assert "time stop and fixed size carry the risk" in r.text
    assert "$0.00" not in r.text
    # Humanized metadata with the new keys.
    assert "Idiosyncratic drop" in r.text and "+4.90%" in r.text
    assert "Selloff volume vs normal" in r.text
    # Sizing card: fixed-fraction rule, scalar does not apply → 1.000.
    assert "fixed 10% of equity" in r.text
    assert "does not apply to this strategy" in r.text
    assert "1.000" in r.text
    # Regime table on the signal date.
    assert "22.2% realized" in r.text and "rank 91%" in r.text
    assert "HY OAS 341 bp" in r.text


def test_signal_detail_vol_scaled_strategy_blocked_by_gate():
    signal = _signal_row(
        strategy_name="momentum_52w_high", suggested_stop=Decimal("159.12"),
        status="rejected", rejected_reason="trend_gate_closed",
        metadata={"closeness_52w": 0.97, "slope_quality": 0.81, "sma_50": 180.0,
                  "sma_200": 171.5, "realized_vol_1y": 0.31, "residual_tilt": 0.12,
                  "residual_return_12m": 0.12},
    )
    client = _client_with_signal(signal, _regime(gate_open=False))
    r = client.get("/signals/42")
    assert r.status_code == 200
    assert "Trend gate closed" in r.text  # humanized rejection reason
    assert "risk 0.75% of equity against the stop" in r.text
    assert "applies to this strategy" in r.text and "0.720" in r.text
    assert "New longs were blocked on this day" in r.text
    assert "risk off" in r.text


# Data-plan gating: cards and columns for feeds that are off collapse, and
# the dashboard explains the absence once.
# -----------------------------------------------------------------------

def _prices_only_plan(monkeypatch):
    from stockscan import config

    monkeypatch.setattr(config.settings, "eodhd_features", "eod,bulk")


def test_dashboard_hides_cards_for_feeds_off_plan(client, monkeypatch):
    _prices_only_plan(monkeypatch)
    r = client.get("/")
    assert r.status_code == 200
    assert "Market News" not in r.text
    assert "Macro this week" not in r.text
    assert "Earnings this week" not in r.text
    assert "Not on current data plan: universe, fundamentals, news, calendar, insider, econ_events" in r.text
    # Headline strip is four stats, regime included.
    assert "Passing signals" in r.text and "Regime" in r.text and "Cash" not in r.text


def test_dashboard_shows_cards_on_full_plan(client, monkeypatch):
    from stockscan import config

    monkeypatch.setattr(config.settings, "eodhd_features", "all")
    r = client.get("/")
    assert "Market News" in r.text
    assert "Macro this week" in r.text
    assert "Earnings this week" in r.text
    assert "Not on current data plan" not in r.text


def test_watchlist_has_no_earnings_columns(client, monkeypatch):
    """Earnings / revisions / insider detail lives on the Analysis page the
    symbol links to, on every data plan."""
    from stockscan import config
    from stockscan.web.routes import watchlist as wl_route

    monkeypatch.setattr(config.settings, "eodhd_features", "all")
    monkeypatch.setattr(wl_route, "list_watchlist", lambda **k: [SimpleNamespace(
        watchlist_id=1, symbol="AAPL", last_close=Decimal("187.20"),
        pct_change_today=0.012, last_volume=1_000_000, target_price=Decimal("200"),
        target_direction="above", alert_enabled=True, target_satisfied=False,
        last_bar_date=date(2026, 9, 18),
    )])
    r = client.get("/watchlist")
    assert r.status_code == 200
    import re

    for header in ("Earnings", "Est revs 30d", "Insider 90d"):
        assert not re.search(r">\s*" + re.escape(header) + r"\s*</th>", r.text)
    assert 'href="/analysis/AAPL"' in r.text
    assert "Analyse" in r.text and 'href="/analysis?list=' in r.text
    # Mobile: the target editor sits behind a plain-text disclosure.
    assert "<summary" in r.text and "edit target" in r.text
    assert "above $200.00" in r.text


def test_analysis_detail_has_no_macro_strip(client):
    from stockscan.web.routes import analysis as an_route

    assert not hasattr(an_route, "upcoming_events")
    r = client.get("/analysis/AAPL")
    assert r.status_code == 200
    assert "Macro this week" not in r.text


def test_signal_detail_has_no_scan_run_card():
    client = _client_with_signal(_signal_row(), _regime())
    r = client.get("/signals/42")
    assert "Scan-run context" not in r.text
    assert "Raw signal.metadata" not in r.text
    assert "Base rates →" in r.text
    # The header keeps the strategy link and the back link; the footer no
    # longer duplicates them.
    import re

    assert not re.search(r"Strategy reference\s*</a>", r.text)
    assert r.text.count("Back to signals") == 1


def test_hedge_pages_render_money_values(client, monkeypatch):
    from stockscan.web.routes import hedge as hedge_route

    pos = SimpleNamespace(
        hedge_position_id=7, symbol="MU", option_side="short", contracts=2,
        option_kind="call", strike=Decimal("130"), status="active",
        last_spot=Decimal("128.5"), last_delta=Decimal("-90"), held_shares=90,
        last_target_shares=90, premium=Decimal("1250"), settlement_spot=None,
        close_reason=None, realized_pnl=None,
    )
    pnl = {"option_open_pnl": 312.5, "realized_hedge_pnl": -40.0,
           "hedge_unrealized_pnl": 15.0, "net_open_pnl": 287.5, "option_value": 937.5}
    monkeypatch.setattr(hedge_route.store, "list_hedge_positions", lambda **k: [pos])
    monkeypatch.setattr(hedge_route.store, "get_heartbeat", lambda **k: None)
    monkeypatch.setattr(hedge_route.service, "live_pnl", lambda p, **k: pnl)
    monkeypatch.setattr(hedge_route, "_default_symbol", lambda session: "")
    r = client.get("/hedge")
    assert r.status_code == 200
    # Shared money(): signed values coloured by sign, premium plain.
    assert "+312" in r.text and ">-25<" in r.text and "+288" in r.text
    assert ">1,250<" in r.text
    assert 'text-bad-600">-25' in r.text

    pos.expiry = __import__("datetime").datetime(2026, 10, 16)
    pos.iv_pct = Decimal("42.0")
    pos.rate_pct = Decimal("4.5")
    pos.band_policy = SimpleNamespace(mode="whalley_wilmott")
    pos.avg_cost = Decimal("127.10")
    monkeypatch.setattr(hedge_route.store, "get_hedge_position", lambda hid, **k: pos)
    monkeypatch.setattr(hedge_route.store, "list_adjustments", lambda hid, **k: [])
    r = client.get("/hedge/7")
    assert r.status_code == 200
    assert "+312.50" in r.text and "-25.00" in r.text and "mark <span" in r.text


# -----------------------------------------------------------------------
# /options: regime controls + book multiplier + macro line in the header,
# σ-distance / yield / contracts on the row, the rank inputs and the
# trigger-class base rate on the card.
# -----------------------------------------------------------------------

def _proposal(**overrides):
    from stockscan.proposals._models import OptionProposal

    fields = dict(
        symbol="AAPL", side="sell_put", expiry_date=date(2026, 9, 25), days_to_expiry=7,
        strike=180.0, delta=-0.15, est_credit=1.25, pct_otm=-8.0, hv_pct=32.0,
        hv_percentile=71.0, move_sigma=-1.8, trend_align=1.0, rank_key=1.8,
        size_weight=0.72, contracts=3, sigma_distance=1.5, credit_yield_ann=36.2,
        day_move_pct=-2.4, day_move_residual_pct=-2.1, days_to_earnings=None,
        earnings_known=True, trend_bucket="up", confluences=("50 EMA $180.40",),
        rationale="Red day (−1.8σ residual, −2.4% raw); sell put 180.",
        score_breakdown={"move_sigma": -1.8, "trend_align": 1.0, "hv_percentile": 71.0, "rank_key": 1.8},
    )
    fields.update(overrides)
    return OptionProposal(**fields)


def _options_run(book, regime, **overrides):
    from stockscan.proposals.service import ProposalRun

    fields = dict(
        as_of=date(2026, 9, 18), regime=regime, candidates=len(book) + 2, book=book,
        book_mult=0.72, macro_events=["CPI Thu", "FOMC Wed"], equity=125_000.0,
    )
    fields.update(overrides)
    return ProposalRun(**fields)


def test_options_page_header_rows_and_card(client, monkeypatch):
    from stockscan.web.routes import options as options_route

    book = [_proposal(), _proposal(symbol="XOM", earnings_known=False, contracts=None)]
    monkeypatch.setattr(options_route, "generate_book", lambda **k: _options_run(book, _regime()))
    monkeypatch.setattr(
        options_route, "trigger_base_rates",
        lambda session=None: {("sell_put", "up", True): (41, 3)},
    )
    r = client.get("/options?n=5")
    assert r.status_code == 200
    # Header: the three regime controls, the book multiplier, the macro line.
    assert "7 closes on side" in r.text and "×0.72" in r.text and "clear" in r.text
    assert "Book size" in r.text and "$125,000 equity" in r.text
    assert "CPI Thu · FOMC Wed inside expiry" in r.text
    assert "2 of 4 candidates" in r.text
    # Row: σ-distance, HV (never labelled IV), yield, contracts.
    assert "1.5σ" in r.text and "HV 32% (rank 71)" in r.text and "36%/yr" in r.text
    assert "3 contracts" in r.text and "size n/a" in r.text
    assert "-2.1% vs sector" in r.text
    assert "IV" not in r.text
    # Card: rank inputs, confluences as a fact, earnings flag, base rate.
    assert "Rank key" in r.text and "1.800" in r.text
    assert "50 EMA $180.40" in r.text
    assert "earnings: unknown" in r.text
    assert "41 proposed, 3 breached (7%)" in r.text
    assert "score" not in r.text.lower().split("why this trade", 1)[1].split("</details>", 1)[0]


def test_options_page_base_rate_needs_thirty(client, monkeypatch):
    from stockscan.web.routes import options as options_route

    monkeypatch.setattr(
        options_route, "generate_book",
        lambda **k: _options_run([_proposal()], _regime(gate_open=False, stress=True)),
    )
    monkeypatch.setattr(
        options_route, "trigger_base_rates",
        lambda session=None: {("sell_put", "up", False): (12, 1)},
    )
    r = client.get("/options")
    assert r.status_code == 200
    assert "n &lt; 30" in r.text
    assert "closed" in r.text and "firing" in r.text
    assert "put-sales skipped; call-sales halved" in r.text


def test_options_page_no_regime_no_book(client, monkeypatch):
    from stockscan.web.routes import options as options_route

    monkeypatch.setattr(
        options_route, "generate_book",
        lambda **k: _options_run([], None, candidates=0, macro_events=[], book_mult=1.0),
    )

    def _boom(session=None):
        raise RuntimeError("relation does not exist")

    monkeypatch.setattr(options_route, "trigger_base_rates", _boom)
    r = client.get("/options?list=3")
    assert r.status_code == 200
    assert "Regime n/a" in r.text and "No qualifying proposals" in r.text
    assert "No high-importance US macro events inside expiry" in r.text
