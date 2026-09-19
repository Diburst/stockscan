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


# ----------------------------------------------------------------------
# Provider feature families (subscription entitlements)
# ----------------------------------------------------------------------
# Each name is one endpoint family a data plan may or may not include.
# ``DataProvider.supports(feature)`` answers "may I call this family?";
# refresh entrypoints check it up front so a downgraded plan skips cleanly
# instead of burning quota on 403s. Configured via EODHD_FEATURES.
ALL_FEATURES: frozenset[str] = frozenset(
    {
        "eod",  # /eod/{symbol} per-symbol history
        "bulk",  # /eod-bulk-last-day/{exchange}
        "universe",  # /fundamentals/GSPC.INDX index components (Fundamentals plan!)
        "fundamentals",  # /fundamentals/{symbol}
        "news",  # /news
        "calendar",  # /calendar/earnings, /calendar/trends
        "insider",  # /insider-transactions
        "econ_events",  # /economic-events
    }
)

# One canonical human-readable reason, reused by CLI notices, refresh
# results, MCP tool responses and UI cards so the wording never drifts.
DISABLED_REASON = "not available on the current data plan (see EODHD_FEATURES)"


class FeatureDisabled(RuntimeError):
    """Raised when a provider method is called for a feature family the
    current subscription does not include. Callers that gate with
    ``provider.supports(...)`` never see it; it is the belt-and-braces
    guard so no network call can slip through."""

    def __init__(self, feature: str) -> None:
        super().__init__(f"provider feature '{feature}' is {DISABLED_REASON}")
        self.feature = feature


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


@dataclass(frozen=True, slots=True)
class MacroRow:
    """A single observation of a scalar macro time series.

    Used for FRED-style series that don't have OHLC structure (e.g.,
    HY OAS, yield-curve spreads, dollar index level). One row = one
    (series, date) observation. Stored in the ``macro_series`` table.
    """

    series_code: str  # e.g., 'BAMLH0A0HYM2'
    as_of_date: date
    value: Decimal
    source: str  # 'fred', etc.


class DataProvider(ABC):
    """Provider contract.

    Methods return iterables of canonical dataclasses. Implementations
    are responsible for translating provider-specific JSON/CSV into these
    types.
    """

    name: str  # 'eodhd', 'polygon', 'stub', etc.

    def supports(self, feature: str) -> bool:
        """Whether this provider may call the given feature family.

        Default: everything (the stub, and any provider without plan
        tiers). EODHD overrides this from ``settings.eodhd_feature_set``.
        """
        return feature in ALL_FEATURES

    def require(self, feature: str) -> None:
        """Raise :class:`FeatureDisabled` unless ``supports(feature)``."""
        if not self.supports(feature):
            raise FeatureDisabled(feature)

    @abstractmethod
    def get_bars(
        self,
        symbol: str,
        start: date,
        end: date,
        interval: str = "1d",
        exchange: str = "US",
    ) -> list[BarRow]:
        """Return bars for `symbol` in [start, end] inclusive.

        `exchange` selects the EODHD-style suffix appended to the ticker
        (e.g., ``"US"`` for /eod/AAPL.US, ``"INDX"`` for /eod/VIX.INDX).
        Providers without a per-exchange addressing scheme should ignore it.
        """

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
            for bar in self.get_bars(symbol, bar_date, bar_date, exchange=exchange)
        ]
