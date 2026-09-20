"""Tests for the sector-relative primitive (stockscan.indicators.relative_strength).

``sector_return`` / ``sector_relative_return`` read the symbol from
``bars.attrs["symbol"]``, resolve its ``$EWSECTOR:<CODE>`` composite through
``stockscan.sectors.store.sector_map`` and fetch the composite's closes through
``stockscan.data.store.get_bars``. Both are patched here; the run-scoped caches
(``_SECTOR_MAP`` / ``_COMPOSITE_CLOSES``) are cleared around every test.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

import stockscan.indicators.relative_strength as srs
from stockscan.indicators import sector_relative_return, sector_return

LOOK = 10
N = 60
START = "2024-01-01"


@pytest.fixture(autouse=True)
def _clean_caches():
    srs.clear_cache()
    yield
    srs.clear_cache()


def _ramp(p_then: float, p_now: float, n: int = N, look: int = LOOK) -> list[float]:
    """Flat at ``p_then``, then linear to ``p_now`` over the last ``look``+1
    bars — so close[-1] / close[-1-look] == p_now / p_then exactly."""
    arr = np.empty(n, dtype=float)
    start = n - 1 - look
    arr[:start] = p_then
    arr[start:] = np.linspace(p_then, p_now, look + 1)
    return arr.tolist()


def _stock_bars(closes: list[float], symbol: str | None = "AAPL", *, hour: int = 21) -> pd.DataFrame:
    """Stock bars with EODHD-style timestamps (NY close in UTC, not midnight)."""
    idx = pd.date_range(START, periods=len(closes), freq="B") + pd.Timedelta(hours=hour)
    idx = pd.DatetimeIndex(idx, tz="UTC")
    df = pd.DataFrame({"close": closes, "adj_close": closes}, index=idx)
    if symbol is not None:
        df.attrs["symbol"] = symbol
    return df


def _composite_frame(closes: list[float]) -> pd.DataFrame:
    """Composite bars as sectors/store writes them: midnight UTC."""
    idx = pd.date_range(START, periods=len(closes), freq="B", tz="UTC")
    return pd.DataFrame({"close": closes}, index=idx)


@pytest.fixture
def wire(monkeypatch):
    """Patch the two DB touch points; returns the call counter."""
    calls = {"map": 0, "bars": 0, "symbols": []}

    def install(sector_map: dict[str, str], composites: dict[str, pd.DataFrame]):
        def fake_sector_map(**_):
            calls["map"] += 1
            return sector_map

        def fake_get_bars(symbol, start=None, end=None, **_):
            calls["bars"] += 1
            calls["symbols"].append(symbol)
            return composites.get(symbol, pd.DataFrame())

        monkeypatch.setattr("stockscan.sectors.store.sector_map", fake_sector_map)
        monkeypatch.setattr("stockscan.data.store.get_bars", fake_get_bars)
        return calls

    return install


def _as_of(bars: pd.DataFrame) -> date:
    return bars.index[-1].date()


# ---------------------------------------------------------------------
# Trailing-return math
# ---------------------------------------------------------------------
def test_sector_return_is_trailing_composite_return(wire):
    wire({"AAPL": "Technology"}, {"$EWSECTOR:TECHNOLOGY": _composite_frame(_ramp(100, 105))})
    bars = _stock_bars(_ramp(100, 120))
    assert sector_return(bars, _as_of(bars), lookback=LOOK) == pytest.approx(0.05)


def test_relative_return_is_stock_minus_sector(wire):
    wire({"AAPL": "Technology"}, {"$EWSECTOR:TECHNOLOGY": _composite_frame(_ramp(100, 105))})
    bars = _stock_bars(_ramp(100, 120))  # +20% vs +5%
    assert sector_relative_return(bars, _as_of(bars), lookback=LOOK) == pytest.approx(0.15)


def test_relative_return_laggard_is_negative(wire):
    wire({"AAPL": "Technology"}, {"$EWSECTOR:TECHNOLOGY": _composite_frame(_ramp(100, 120))})
    bars = _stock_bars(_ramp(100, 105))  # +5% vs +20%
    assert sector_relative_return(bars, _as_of(bars), lookback=LOOK) == pytest.approx(-0.15)


def test_resilient_in_falling_sector_is_positive(wire):
    wire({"AAPL": "Technology"}, {"$EWSECTOR:TECHNOLOGY": _composite_frame(_ramp(100, 80))})
    bars = _stock_bars(_ramp(100, 95))  # −5% vs −20%
    assert sector_return(bars, _as_of(bars), lookback=LOOK) == pytest.approx(-0.20)
    assert sector_relative_return(bars, _as_of(bars), lookback=LOOK) == pytest.approx(0.15)


def test_relative_return_uses_adj_close_not_close(wire):
    wire({"AAPL": "Technology"}, {"$EWSECTOR:TECHNOLOGY": _composite_frame(_ramp(100, 100))})
    bars = _stock_bars(_ramp(100, 110))
    bars["close"] = _ramp(100, 150)  # unadjusted column must be ignored
    assert sector_relative_return(bars, _as_of(bars), lookback=LOOK) == pytest.approx(0.10)


def test_sector_code_is_slugified(wire):
    calls = wire(
        {"JPM": "Financial Services"},
        {"$EWSECTOR:FINANCIAL_SERVICES": _composite_frame(_ramp(100, 110))},
    )
    bars = _stock_bars(_ramp(100, 100), symbol="JPM")
    assert sector_return(bars, _as_of(bars), lookback=LOOK) == pytest.approx(0.10)
    assert calls["symbols"] == ["$EWSECTOR:FINANCIAL_SERVICES"]


def test_symbol_column_fallback_when_attrs_missing(wire):
    wire({"AAPL": "Technology"}, {"$EWSECTOR:TECHNOLOGY": _composite_frame(_ramp(100, 105))})
    bars = _stock_bars(_ramp(100, 120), symbol=None)
    bars["symbol"] = "AAPL"
    assert sector_return(bars, _as_of(bars), lookback=LOOK) == pytest.approx(0.05)


# ---------------------------------------------------------------------
# Abstain cases
# ---------------------------------------------------------------------
def test_none_when_no_symbol(wire):
    calls = wire({"AAPL": "Technology"}, {})
    bars = _stock_bars(_ramp(100, 120), symbol=None)
    assert sector_return(bars, _as_of(bars), lookback=LOOK) is None
    assert sector_relative_return(bars, _as_of(bars), lookback=LOOK) is None
    assert calls["bars"] == 0


def test_none_when_symbol_has_no_sector(wire):
    calls = wire({"MSFT": "Technology"}, {"$EWSECTOR:TECHNOLOGY": _composite_frame(_ramp(100, 105))})
    bars = _stock_bars(_ramp(100, 120), symbol="AAPL")
    assert sector_return(bars, _as_of(bars), lookback=LOOK) is None
    assert sector_relative_return(bars, _as_of(bars), lookback=LOOK) is None
    assert calls["bars"] == 0


def test_none_when_composite_has_no_bars(wire):
    wire({"AAPL": "Technology"}, {})  # get_bars returns an empty frame
    bars = _stock_bars(_ramp(100, 120))
    assert sector_return(bars, _as_of(bars), lookback=LOOK) is None
    assert sector_relative_return(bars, _as_of(bars), lookback=LOOK) is None


def test_none_when_composite_too_short(wire):
    # Exactly ``lookback`` bars: need lookback + 1 to form a return.
    wire({"AAPL": "Technology"}, {"$EWSECTOR:TECHNOLOGY": _composite_frame([100.0] * LOOK)})
    bars = _stock_bars(_ramp(100, 120))
    assert sector_return(bars, _as_of(bars), lookback=LOOK) is None
    wire({"AAPL": "Technology"}, {"$EWSECTOR:TECHNOLOGY": _composite_frame([100.0] * (LOOK + 1))})
    srs.clear_cache()
    assert sector_return(bars, _as_of(bars), lookback=LOOK) == pytest.approx(0.0)


def test_none_when_stock_too_short(wire):
    wire({"AAPL": "Technology"}, {"$EWSECTOR:TECHNOLOGY": _composite_frame(_ramp(100, 105))})
    bars = _stock_bars([100.0] * LOOK)
    as_of = pd.date_range(START, periods=N, freq="B")[-1].date()  # composite's last bar
    assert sector_return(bars, as_of, lookback=LOOK) == pytest.approx(0.05)
    assert sector_relative_return(bars, as_of, lookback=LOOK) is None


def test_none_when_composite_base_is_zero(wire):
    wire({"AAPL": "Technology"}, {"$EWSECTOR:TECHNOLOGY": _composite_frame(_ramp(0.0, 105))})
    bars = _stock_bars(_ramp(100, 120))
    assert sector_return(bars, _as_of(bars), lookback=LOOK) is None


def test_none_when_as_of_precedes_composite(wire):
    wire({"AAPL": "Technology"}, {"$EWSECTOR:TECHNOLOGY": _composite_frame(_ramp(100, 105))})
    bars = _stock_bars(_ramp(100, 120))
    assert sector_return(bars, date(2023, 12, 1), lookback=LOOK) is None


# ---------------------------------------------------------------------
# No look-ahead
# ---------------------------------------------------------------------
def test_composite_is_sliced_to_as_of(wire):
    # Composite: flat 100 through bar N-11, then a ramp to 200 at the end.
    comp = _ramp(100, 200)
    wire({"AAPL": "Technology"}, {"$EWSECTOR:TECHNOLOGY": _composite_frame(comp)})
    bars = _stock_bars(_ramp(100, 100))
    # Asking as of the last flat bar must see no composite move at all.
    flat_as_of = bars.index[N - 2 - LOOK].date()
    assert sector_return(bars, flat_as_of, lookback=LOOK) == pytest.approx(0.0)
    # And the final bar sees the full ramp.
    assert sector_return(bars, _as_of(bars), lookback=LOOK) == pytest.approx(1.0)


def test_slice_is_by_calendar_date_not_intraday_time(wire):
    """Stock bars sit at NY close (21:00 UTC); composites at midnight UTC.
    An ``as_of`` date must include that day's composite bar."""
    comp = _ramp(100, 105)
    wire({"AAPL": "Technology"}, {"$EWSECTOR:TECHNOLOGY": _composite_frame(comp)})
    bars = _stock_bars(_ramp(100, 120), hour=21)
    as_of = _as_of(bars)
    cached = srs._composite_closes("$EWSECTOR:TECHNOLOGY", as_of)
    assert cached is not None
    assert cached.index[-1] == pd.Timestamp(as_of)
    assert cached.index.tz is None
    assert len(cached) == N


def test_slice_matches_date_mask_every_day(wire):
    comp_df = _composite_frame(list(np.linspace(100, 130, N)))
    wire({"AAPL": "Technology"}, {"$EWSECTOR:TECHNOLOGY": comp_df})
    srs._composite_symbol_for("AAPL")
    for ts in comp_df.index:
        d = ts.date()
        out = srs._composite_closes("$EWSECTOR:TECHNOLOGY", d)
        expected = comp_df["close"][comp_df.index.date <= d]
        assert out is not None
        assert len(out) == len(expected)
        assert float(out.iloc[-1]) == float(expected.iloc[-1])


# ---------------------------------------------------------------------
# Cache reuse
# ---------------------------------------------------------------------
def test_sector_map_and_composite_fetched_once_per_run(wire):
    calls = wire(
        {"AAPL": "Technology", "MSFT": "Technology", "JPM": "Financial Services"},
        {
            "$EWSECTOR:TECHNOLOGY": _composite_frame(_ramp(100, 105)),
            "$EWSECTOR:FINANCIAL_SERVICES": _composite_frame(_ramp(100, 110)),
        },
    )
    for sym in ("AAPL", "MSFT", "AAPL", "JPM", "MSFT"):
        bars = _stock_bars(_ramp(100, 120), symbol=sym)
        for cut in (N - 1, N - 5, N - 1):
            assert sector_relative_return(bars, bars.index[cut].date(), lookback=LOOK) is not None
    assert calls["map"] == 1
    assert calls["bars"] == 2  # one fetch per distinct composite
    assert sorted(calls["symbols"]) == ["$EWSECTOR:FINANCIAL_SERVICES", "$EWSECTOR:TECHNOLOGY"]
    assert set(srs._COMPOSITE_CLOSES) == {"$EWSECTOR:TECHNOLOGY", "$EWSECTOR:FINANCIAL_SERVICES"}


def test_empty_composite_is_cached_too(wire):
    calls = wire({"AAPL": "Technology"}, {})
    bars = _stock_bars(_ramp(100, 120))
    for _ in range(3):
        assert sector_return(bars, _as_of(bars), lookback=LOOK) is None
    assert calls["bars"] == 1
    assert srs._COMPOSITE_CLOSES["$EWSECTOR:TECHNOLOGY"].empty


def test_clear_cache_forces_refetch(wire):
    calls = wire({"AAPL": "Technology"}, {"$EWSECTOR:TECHNOLOGY": _composite_frame(_ramp(100, 105))})
    bars = _stock_bars(_ramp(100, 120))
    sector_return(bars, _as_of(bars), lookback=LOOK)
    assert (calls["map"], calls["bars"]) == (1, 1)
    assert srs._COMPOSITE_CLOSES

    srs.clear_cache()
    assert srs._COMPOSITE_CLOSES == {}
    assert srs._SECTOR_MAP is None
    sector_return(bars, _as_of(bars), lookback=LOOK)
    assert (calls["map"], calls["bars"]) == (2, 2)


def test_cached_closes_are_pre_normalised(wire):
    wire({"AAPL": "Technology"}, {"$EWSECTOR:TECHNOLOGY": _composite_frame(_ramp(100, 105))})
    bars = _stock_bars(_ramp(100, 120))
    sector_return(bars, _as_of(bars), lookback=LOOK)
    cached = srs._COMPOSITE_CLOSES["$EWSECTOR:TECHNOLOGY"]
    assert cached.index.tz is None
    assert (cached.index == cached.index.normalize()).all()
    assert cached.dtype == float
