"""EODHD_FEATURES — graceful degradation on a reduced data plan.

Covers:

* config parsing of the feature list (default = everything);
* the provider gate: a disabled family never reaches the network;
* each refresh entrypoint short-circuits with ``skipped``/``skipped_reason``
  and makes zero provider calls;
* the MCP write tools return a structured ``feature_disabled`` refusal;
* ``refresh_universe`` routes to the Wikipedia fallback, whose parser and
  incremental merge are exercised on a page-shaped fixture (rowspan'd
  change dates, footnotes, ``BRK.B`` → ``BRK-B``).
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import httpx
import pytest

from stockscan.data.providers.base import ALL_FEATURES, DISABLED_REASON, FeatureDisabled
from stockscan.data.providers.eodhd import EODHDProvider


# ----------------------------------------------------------------------
# config
# ----------------------------------------------------------------------
def test_feature_set_defaults_to_everything() -> None:
    from stockscan.config import Settings

    assert Settings(_env_file=None).eodhd_feature_set == ALL_FEATURES
    assert Settings(eodhd_features="all", _env_file=None).eodhd_feature_set == ALL_FEATURES
    assert Settings(eodhd_features="", _env_file=None).eodhd_feature_set == ALL_FEATURES


def test_feature_set_parses_explicit_list() -> None:
    from stockscan.config import Settings

    s = Settings(eodhd_features=" EOD, bulk ,", _env_file=None)
    assert s.eodhd_feature_set == frozenset({"eod", "bulk"})


def test_config_warnings_mention_excluded_families() -> None:
    from stockscan import config as cfg

    s = cfg.Settings(eodhd_features="eod,bulk,bogus", eodhd_api_key="k", _env_file=None)
    with patch.object(cfg, "settings", s):
        lines = cfg.config_warnings()
    assert any("unknown entries bogus" in ln for ln in lines)
    assert any("excludes" in ln and "fundamentals" in ln and "news" in ln for ln in lines)


# ----------------------------------------------------------------------
# provider gate
# ----------------------------------------------------------------------
def _provider(features: str) -> EODHDProvider:
    calls: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json=[])

    with patch("stockscan.data.providers.eodhd.settings") as st:
        st.eodhd_feature_set = frozenset(features.split(","))
        st.eodhd_base_url = "https://example.test"
        p = EODHDProvider(api_key="k", transport=httpx.MockTransport(_handler))
    p._calls = calls  # type: ignore[attr-defined]
    return p


def test_eod_only_provider_blocks_offplan_families_before_network() -> None:
    p = _provider("eod,bulk")
    assert p.supports("eod") and p.supports("bulk")
    for feat in ALL_FEATURES - {"eod", "bulk"}:
        assert not p.supports(feat)

    with pytest.raises(FeatureDisabled) as ei:
        p.get_fundamentals("AAPL")
    assert ei.value.feature == "fundamentals"
    assert DISABLED_REASON in str(ei.value)
    with pytest.raises(FeatureDisabled):
        p.get_sp500_constituents()
    with pytest.raises(FeatureDisabled):
        p.get_news(symbol="AAPL", from_date=date(2026, 1, 1), to_date=date(2026, 1, 2))
    with pytest.raises(FeatureDisabled):
        p.get_insider_transactions(symbol="AAPL")
    with pytest.raises(FeatureDisabled):
        p.get_earnings(["AAPL"], date(2026, 1, 1), date(2026, 3, 1))
    assert p._calls == []  # type: ignore[attr-defined]

    # The entitled families still go out.
    p.get_eod_bulk(date(2026, 9, 18))
    assert p._calls == ["/eod-bulk-last-day/US"]  # type: ignore[attr-defined]


def test_default_provider_supports_everything() -> None:
    p = _provider(",".join(sorted(ALL_FEATURES)))
    assert all(p.supports(f) for f in ALL_FEATURES)


def test_stub_provider_supports_everything() -> None:
    from stockscan.data.providers.stub import StubProvider

    assert all(StubProvider().supports(f) for f in ALL_FEATURES)


# ----------------------------------------------------------------------
# refresh entrypoints: zero calls, explicit skip
# ----------------------------------------------------------------------
class _Off:
    """A provider that reports every family off — any method call is a bug."""

    name = "off"

    def supports(self, feature: str) -> bool:
        return False

    def __getattr__(self, item: str):
        raise AssertionError(f"provider.{item} must not be called when the feature is off")


def test_refresh_fundamentals_skips_without_calls() -> None:
    from stockscan.fundamentals.refresh import refresh_fundamentals

    assert refresh_fundamentals(_Off(), ["AAPL", "MSFT"]) == {"AAPL": "skipped", "MSFT": "skipped"}


def test_refresh_news_skips_without_calls() -> None:
    from stockscan.news import refresh as nr

    with patch.object(nr, "last_fetched_at", return_value=None):
        r = nr.refresh_news(_Off(), watchlist_symbols=["AAPL"])
    assert r.skipped_reason == DISABLED_REASON
    assert r.api_calls == 0 and r.articles_upserted == 0


def test_fetch_article_content_skips_without_calls() -> None:
    from stockscan.news.refresh import fetch_article_content

    art = MagicMock(link="https://x", symbols=["AAPL"], tags=[])
    assert fetch_article_content(_Off(), art) is None


def test_refresh_earnings_skips_without_calls() -> None:
    from stockscan.earnings.refresh import refresh_earnings

    r = refresh_earnings(_Off(), ["AAPL"])
    assert r.skipped_reason == DISABLED_REASON
    assert r.error is None and r.calendar_upserted == 0


def test_refresh_econ_events_skips_without_calls() -> None:
    from stockscan.econ_events.refresh import refresh_economic_events

    r = refresh_economic_events(_Off())
    assert r.skipped_reason == DISABLED_REASON and r.upserted == 0


def test_refresh_insider_skips_without_cooldown_bookkeeping() -> None:
    from stockscan.insider import refresh as ir

    with patch.object(ir, "can_refresh") as can, patch.object(ir, "start_refresh") as start:
        r1 = ir.refresh_insider_for_watchlist(_Off(), ["AAPL"])
        r2 = ir.refresh_insider_for_symbol(_Off(), "AAPL")
    assert r1.skipped and r1.skipped_reason == DISABLED_REASON
    assert r2.skipped and r2.skipped_reason == DISABLED_REASON
    # No cooldown row is written, so an upgrade isn't followed by a 23h wait.
    can.assert_not_called()
    start.assert_not_called()


# ----------------------------------------------------------------------
# MCP tools
# ----------------------------------------------------------------------
def test_mcp_refresh_tools_return_structured_refusal() -> None:
    from contextlib import contextmanager

    from stockscan.mcp.tools import data as t

    @contextmanager
    def _ctx():
        yield _Off()

    with patch.object(t, "provider_ctx", _ctx), patch.object(
        t, "watchlist_symbols", return_value=["AAPL"]
    ):
        for fn, feat in (
            (lambda: t.refresh_fundamentals("AAPL"), "fundamentals"),
            (lambda: t.refresh_news(), "news"),
            (lambda: t.refresh_earnings("AAPL"), "calendar"),
            (lambda: t.refresh_insider("AAPL"), "insider"),
        ):
            out = fn()
            assert out["error"] == "feature_disabled", out
            assert out["feature"] == feat
            assert DISABLED_REASON in out["detail"]


# ----------------------------------------------------------------------
# universe: Wikipedia fallback
# ----------------------------------------------------------------------
_PAGE = """
<html><body>
<table class="wikitable" id="constituents">
<tr><th>Symbol</th><th>Security</th><th>GICS Sector</th><th>GICS Sub-Industry</th>
<th>Headquarters Location</th><th>Date added</th><th>CIK</th><th>Founded</th></tr>
<tr><td><a href="#">MMM</a></td><td>3M</td><td>Industrials</td><td>Conglomerates</td>
<td>Saint Paul</td><td>1957-03-04</td><td>0000066740</td><td>1902</td></tr>
<tr><td>BRK.B</td><td>Berkshire</td><td>Financials</td><td>Multi</td><td>Omaha</td>
<td>2010-02-16</td><td>1</td><td>1839</td></tr>
<tr><td>HONA<sup>[3]</sup></td><td>Honeywell Aerospace</td><td>Industrials</td><td>Aero</td>
<td>Charlotte</td><td></td><td>2</td><td>2026</td></tr>
</table>
<table class="wikitable" id="changes">
<tr><th rowspan="2">Effective Date</th><th colspan="2">Added</th><th colspan="2">Removed</th>
<th rowspan="2">Reason</th></tr>
<tr><th>Ticker</th><th>Security</th><th>Ticker</th><th>Security</th></tr>
<tr><td rowspan="2">June 30, 2026</td><td></td><td></td><td>CAG</td><td>Conagra</td>
<td rowspan="2">Market cap.</td></tr>
<tr><td>HONA</td><td>Honeywell Aerospace</td><td></td><td></td></tr>
<tr><td>March 2, 2026</td><td>XYZ</td><td>Xyz Co</td><td>OLD</td><td>Old Co</td><td>Spin-off.</td></tr>
</table>
</body></html>
"""


def test_wikipedia_parser_handles_rowspan_footnotes_and_share_classes() -> None:
    from stockscan.universe.wikipedia import parse_sp500_page

    u = parse_sp500_page(_PAGE)
    assert [r.symbol for r in u.roster] == ["MMM", "BRK-B", "HONA"]
    assert u.roster[0].date_added == date(1957, 3, 4)
    assert u.roster[2].date_added is None
    assert [(c.effective, c.added, c.removed) for c in u.changes] == [
        (date(2026, 6, 30), None, "CAG"),
        (date(2026, 6, 30), "HONA", None),
        (date(2026, 3, 2), "XYZ", "OLD"),
    ]


def test_wikipedia_parser_rejects_page_without_roster() -> None:
    from stockscan.universe.wikipedia import parse_sp500_page

    with pytest.raises(ValueError):
        parse_sp500_page("<html><table><tr><th>Nope</th></tr></table></html>")


class _Session:
    """Minimal fake: answers the open-interval query, records writes."""

    def __init__(self, open_rows: list[tuple[str, date]]) -> None:
        self.open_rows = open_rows
        self.writes: list[tuple[str, list[dict]]] = []

    def execute(self, sql, params=None):
        text = str(sql)
        if text.lstrip().upper().startswith("SELECT"):
            return list(self.open_rows)
        self.writes.append(("INSERT" if "INSERT" in text.upper() else "UPDATE", params))
        return None


def test_wikipedia_merge_only_touches_the_frontier() -> None:
    from stockscan.universe.wikipedia import merge_universe, parse_sp500_page

    uni = parse_sp500_page(_PAGE)
    # DB already knows MMM and BRK-B (open) plus CAG (open — since removed).
    s = _Session([("MMM", date(1957, 3, 4)), ("BRK-B", date(2010, 2, 16)), ("CAG", date(1983, 1, 1))])
    out = merge_universe(uni, session=s, today=date(2026, 9, 19))

    assert out.opened == ("HONA",)
    assert out.closed == ("CAG",)
    kinds = {k for k, _ in s.writes}
    assert kinds == {"INSERT", "UPDATE"}
    inserts = next(p for k, p in s.writes if k == "INSERT")
    updates = next(p for k, p in s.writes if k == "UPDATE")
    # HONA has no "Date added" on the roster → dated from the changes log.
    assert inserts == [{"symbol": "HONA", "joined_date": date(2026, 6, 30)}]
    assert updates == [{"symbol": "CAG", "left_date": date(2026, 6, 30)}]


def test_wikipedia_merge_is_a_noop_when_in_sync() -> None:
    from stockscan.universe.wikipedia import merge_universe, parse_sp500_page

    uni = parse_sp500_page(_PAGE)
    s = _Session([("MMM", date(1957, 3, 4)), ("BRK-B", date(2010, 2, 16)), ("HONA", date(2026, 6, 30))])
    out = merge_universe(uni, session=s, today=date(2026, 9, 19))
    assert out.rows_touched == 0 and s.writes == []


def test_wikipedia_refresh_refuses_a_suspiciously_small_roster() -> None:
    from stockscan.universe.wikipedia import refresh_universe_from_wikipedia

    with pytest.raises(ValueError, match="refusing to merge"):
        refresh_universe_from_wikipedia(html=_PAGE, session=_Session([]))


def test_refresh_universe_routes_to_wikipedia_when_feature_off() -> None:
    from stockscan.universe import sp500

    summary = MagicMock(rows_touched=3)
    with patch(
        "stockscan.universe.wikipedia.refresh_universe_from_wikipedia", return_value=summary
    ) as wk:
        assert sp500.refresh_universe(_Off(), session=MagicMock()) == 3
    wk.assert_called_once()


# ----------------------------------------------------------------------
# CLI: the commands themselves, driven end-to-end through Typer
# ----------------------------------------------------------------------
def test_cli_refresh_commands_on_prices_only_plan() -> None:
    """`refresh universe` routes to Wikipedia; `refresh fundamentals` and
    `refresh news` print the plan notice and exit 0 (cron-safe)."""
    from typer.testing import CliRunner

    from stockscan import cli

    class _PricesOnly(_Off):
        def supports(self, feature: str) -> bool:
            return feature in ("eod", "bulk")

        def close(self) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    runner = CliRunner()
    with patch.object(cli, "_provider", return_value=_PricesOnly()), patch.object(
        cli, "refresh_universe", return_value=7
    ) as ru, patch.object(cli, "EODHDProvider", return_value=_PricesOnly()), patch.object(
        cli, "settings", MagicMock(eodhd_api_key=MagicMock(get_secret_value=lambda: "k"))
    ):
        r = runner.invoke(cli.app, ["refresh", "universe"])
        assert r.exit_code == 0, r.output
        assert "Wikipedia" in r.output and "7 membership rows" in r.output
        ru.assert_called_once()

        r = runner.invoke(cli.app, ["refresh", "fundamentals", "AAPL"])
        assert r.exit_code == 0, r.output
        assert "skipped" in r.output and DISABLED_REASON in r.output

        r = runner.invoke(cli.app, ["refresh", "news"])
        assert r.exit_code == 0, r.output
        assert "skipped" in r.output
