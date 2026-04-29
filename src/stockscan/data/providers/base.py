"""Data provider abstract base class.

Every market-data source (EODHD, Polygon, Tiingo, the stub) implements this
contract. Application code only depends on `DataProvider`, never on a
concrete client — swapping providers is a one-line change in the wiring.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class BarRow:
    """A single OHLCV bar in our canonical form."""

    symbol: str
    bar_ts: datetime  # timezone-aware; close timestamp for daily bars
    interval: str  # '1d', '1h', '5m', etc.
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    adj_close: Decimal
    volume: int
    source: str  # provider identifier ('eodhd', 'stub', etc.)


@dataclass(frozen=True, slots=True)
class EarningsRow:
    symbol: str
    report_date: date
    time_of_day: str  # 'bmo' | 'amc' | 'unknown'
    estimate: Decimal | None
    actual: Decimal | None


@dataclass(frozen=True, slots=True)
class UniverseMember:
    symbol: str
    joined_date: date
    left_date: date | None  # None if currently a member


class DataProvider(ABC):
    """Provider contract.

    Methods return iterables of canonical dataclasses. Implementations
    are responsible for translating provider-specific JSON/CSV into these
    types.
    """

    name: str  # 'eodhd', 'polygon', 'stub', etc.

    @abstractmethod
    def get_bars(
        self,
        symbol: str,
        start: date,
        end: date,
        interval: str = "1d",
    ) -> list[BarRow]:
        """Return bars for `symbol` in [start, end] inclusive."""

    @abstractmethod
    def get_sp500_constituents(self) -> list[UniverseMember]:
        """Current S&P 500 membership."""

    @abstractmethod
    def get_sp500_historical_constituents(self) -> list[UniverseMember]:
        """All historical S&P 500 membership intervals (joined/left)."""

    @abstractmethod
    def get_earnings(
        self,
        symbols: list[str],
        start: date,
        end: date,
    ) -> list[EarningsRow]:
        """Earnings calendar for `symbols` in [start, end]."""

    def get_fundamentals(self, symbol: str) -> dict[str, object] | None:
        """Full fundamentals payload for `symbol`, or None if unavailable.

        Returns the provider's raw response shape — interpretation lives in
        the fundamentals store layer where we pluck out specific fields.
        Default: returns None (subclasses override).
        """
        return None

    def get_eod_bulk(
        self,
        bar_date: date,
        exchange: str = "US",
        symbols: list[str] | None = None,
    ) -> list[BarRow]:
        """One API call returning all-symbol EOD for a single trading day.

        Default implementation falls back to per-symbol fetches; concrete
        providers should override with their bulk endpoint where available
        (e.g., EODHD's /eod-bulk-last-day/{exchange}). For daily refreshes
        of large universes this is the difference between 1 API call and
        N (where N = universe size).

        If `symbols` is None, returns bars for every symbol on the exchange
        for that date (the entire universe).
        """
        if symbols is None:
            return []
        return [
            bar
            for symbol in symbols
            for bar in self.get_bars(symbol, bar_date, bar_date)
        ]
