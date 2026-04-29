"""Market-data provider clients."""

from stockscan.data.providers.base import (
    BarRow,
    DataProvider,
    EarningsRow,
    MacroRow,
    UniverseMember,
)
from stockscan.data.providers.eodhd import EODHDProvider
from stockscan.data.providers.fred import FredProvider
from stockscan.data.providers.stub import StubProvider

__all__ = [
    "BarRow",
    "DataProvider",
    "EODHDProvider",
    "EarningsRow",
    "FredProvider",
    "MacroRow",
    "StubProvider",
    "UniverseMember",
]
