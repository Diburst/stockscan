"""EODHD provider: exchange parameter wires through to the URL.

The motivating use case is fetching VIX as ``/eod/VIX.INDX`` for the v2
regime composite. Equities still hit ``/eod/{SYMBOL}.US`` by default; nothing
about the legacy code path changes if no caller passes ``exchange``.

Uses ``httpx.MockTransport`` to capture the requested URL without touching
the network. The transport is injected via the new ``transport`` kwarg on
``EODHDProvider.__init__``.
"""

from __future__ import annotations

from datetime import date

import httpx
import pytest

from stockscan.data.providers.eodhd import EODHDProvider
from stockscan.data.providers.stub import StubProvider

# A small canned response that mimics one trading day from EODHD's /eod/* endpoint.
_EOD_FIXTURE = [
    {
        "date": "2026-04-27",
        "open": 100.50,
        "high": 101.25,
        "low": 100.00,
        "close": 101.00,
        "adjusted_close": 101.00,
        "volume": 1_234_567,
    }
]


def _capturing_transport(captured: list[str]) -> httpx.MockTransport:
    """A MockTransport that appends each requested path to `captured` and
    returns the canned EOD fixture."""

    def _handler(request: httpx.Request) -> httpx.Response:
        # `request.url.path` is the path WITHOUT the query string. That's
        # what we want — we're asserting URL routing, not query encoding.
        captured.append(request.url.path)
        return httpx.Response(200, json=_EOD_FIXTURE)

    return httpx.MockTransport(_handler)


def _provider(transport: httpx.MockTransport) -> EODHDProvider:
    """Construct a provider with no real API key, pointed at a fake base URL."""
    return EODHDProvider(
        api_key="test-key",
        base_url="https://eodhd.test/api",
        transport=transport,
    )


# --------------------------------------------------------------------------
# URL routing
# --------------------------------------------------------------------------
def test_default_exchange_is_us() -> None:
    paths: list[str] = []
    p = _provider(_capturing_transport(paths))
    p.get_bars("AAPL", date(2026, 4, 1), date(2026, 4, 30))
    assert paths == ["/api/eod/AAPL.US"]


def test_indx_exchange_for_vix() -> None:
    paths: list[str] = []
    p = _provider(_capturing_transport(paths))
    p.get_bars("VIX", date(2026, 4, 1), date(2026, 4, 30), exchange="INDX")
    assert paths == ["/api/eod/VIX.INDX"]


def test_arbitrary_exchange_value_is_used_verbatim() -> None:
    """No allowlist on exchange — caller is responsible for passing a valid
    EODHD suffix. We just splice it into the URL."""
    paths: list[str] = []
    p = _provider(_capturing_transport(paths))
    p.get_bars("BARC", date(2026, 4, 1), date(2026, 4, 30), exchange="LSE")
    assert paths == ["/api/eod/BARC.LSE"]


# --------------------------------------------------------------------------
# Bar parsing is unchanged regardless of exchange
# --------------------------------------------------------------------------
def test_indx_response_parses_into_canonical_barrow() -> None:
    p = _provider(_capturing_transport([]))
    bars = p.get_bars("VIX", date(2026, 4, 27), date(2026, 4, 27), exchange="INDX")
    assert len(bars) == 1
    bar = bars[0]
    assert bar.symbol == "VIX"
    assert bar.interval == "1d"
    assert bar.source == "eodhd"
    assert float(bar.close) == 101.00
    # Daily bar timestamp is 16:00 NY (close), converted to UTC.
    assert bar.bar_ts.tzinfo is not None


def test_intraday_interval_still_rejected() -> None:
    """The exchange knob doesn't accidentally enable intraday bars."""
    p = _provider(_capturing_transport([]))
    with pytest.raises(NotImplementedError):
        p.get_bars("AAPL", date(2026, 4, 1), date(2026, 4, 30), interval="5m")


# --------------------------------------------------------------------------
# Stub provider accepts (and ignores) the exchange kwarg
# --------------------------------------------------------------------------
def test_stub_provider_accepts_exchange_kwarg() -> None:
    """The ABC declares ``exchange``; subclasses must accept it. The stub
    is exchange-agnostic, so the parameter is silently ignored."""
    stub = StubProvider(bars=[])
    # Should not raise on either default or explicit exchange.
    assert stub.get_bars("AAPL", date(2026, 1, 1), date(2026, 1, 2)) == []
    assert stub.get_bars("VIX", date(2026, 1, 1), date(2026, 1, 2), exchange="INDX") == []


# --------------------------------------------------------------------------
# Backfill threads exchange through to the provider
# --------------------------------------------------------------------------
def test_backfill_symbol_forwards_exchange(monkeypatch: pytest.MonkeyPatch) -> None:
    """``backfill_symbol(..., exchange="INDX")`` must reach the provider.

    We use a recording fake provider rather than the EODHD mock transport
    here because backfill_symbol also calls ``upsert_bars`` and
    ``latest_bar_date``, which we want to short-circuit without standing
    up a real DB.
    """
    from stockscan.data import backfill as backfill_mod
    from stockscan.data.providers.base import BarRow, DataProvider, EarningsRow, UniverseMember

    # Avoid hitting the DB.
    monkeypatch.setattr(backfill_mod, "latest_bar_date", lambda *_a, **_kw: None)
    monkeypatch.setattr(backfill_mod, "upsert_bars", lambda rows, **_kw: len(list(rows)))

    captured: list[str] = []

    class _RecordingProvider(DataProvider):
        name = "recording"

        def get_bars(  # type: ignore[override]
            self,
            symbol: str,
            start: date,
            end: date,
            interval: str = "1d",
            exchange: str = "US",
        ) -> list[BarRow]:
            captured.append(exchange)
            return []

        def get_sp500_constituents(self) -> list[UniverseMember]:
            return []

        def get_sp500_historical_constituents(self) -> list[UniverseMember]:
            return []

        def get_earnings(self, symbols: list[str], start: date, end: date) -> list[EarningsRow]:
            return []

    backfill_mod.backfill_symbol(
        _RecordingProvider(),
        "VIX",
        start=date(2026, 1, 1),
        end=date(2026, 1, 5),
        exchange="INDX",
    )
    assert captured == ["INDX"]
