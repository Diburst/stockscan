"""Stub provider used by tests and local development.

Serves bars from a small bundled CSV (or an in-memory list passed to the
constructor). Implements the full DataProvider contract so the rest of the
pipeline can be exercised end-to-end without an EODHD account.
"""

from __future__ import annotations

import csv
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from stockscan.data.providers.base import (
    BarRow,
    DataProvider,
    EarningsRow,
    UniverseMember,
)

NY_TZ = ZoneInfo("America/New_York")
DAILY_CLOSE_HOUR = 16

DEFAULT_FIXTURE = Path(__file__).resolve().parents[3].parent / "tests" / "data" / "sample_bars.csv"


class StubProvider(DataProvider):
    """In-memory provider; defaults to bundled sample CSV but accepts overrides."""

    name = "stub"

    def __init__(
        self,
        bars: list[BarRow] | None = None,
        constituents: list[UniverseMember] | None = None,
        earnings: list[EarningsRow] | None = None,
    ) -> None:
        if bars is None:
            bars = self._load_default_bars()
        self._bars = bars
        self._constituents = constituents if constituents is not None else [
            UniverseMember(symbol="AAPL", joined_date=date(1982, 11, 30), left_date=None),
            UniverseMember(symbol="MSFT", joined_date=date(1994, 6, 1), left_date=None),
            UniverseMember(symbol="SPY", joined_date=date(1993, 1, 22), left_date=None),
        ]
        self._earnings = earnings or []

    @staticmethod
    def _load_default_bars() -> list[BarRow]:
        if not DEFAULT_FIXTURE.exists():
            return []
        out: list[BarRow] = []
        with DEFAULT_FIXTURE.open() as f:
            reader = csv.DictReader(f)
            for row in reader:
                d = date.fromisoformat(row["date"])
                ts = datetime(d.year, d.month, d.day, DAILY_CLOSE_HOUR, tzinfo=NY_TZ)
                out.append(
                    BarRow(
                        symbol=row["symbol"],
                        bar_ts=ts.astimezone(timezone.utc),
                        interval="1d",
                        open=Decimal(row["open"]),
                        high=Decimal(row["high"]),
                        low=Decimal(row["low"]),
                        close=Decimal(row["close"]),
                        adj_close=Decimal(row.get("adj_close") or row["close"]),
                        volume=int(row["volume"]),
                        source="stub",
                    )
                )
        return out

    # --------------- DataProvider contract ---------------
    def get_bars(
        self,
        symbol: str,
        start: date,
        end: date,
        interval: str = "1d",
    ) -> list[BarRow]:
        return [
            b
            for b in self._bars
            if b.symbol == symbol and start <= b.bar_ts.date() <= end and b.interval == interval
        ]

    def get_sp500_constituents(self) -> list[UniverseMember]:
        return [m for m in self._constituents if m.left_date is None]

    def get_sp500_historical_constituents(self) -> list[UniverseMember]:
        return list(self._constituents)

    def get_earnings(
        self,
        symbols: list[str],
        start: date,
        end: date,
    ) -> list[EarningsRow]:
        sset = set(symbols)
        return [e for e in self._earnings if e.symbol in sset and start <= e.report_date <= end]

    def get_eod_bulk(
        self,
        bar_date: date,
        exchange: str = "US",
        symbols: list[str] | None = None,
    ) -> list[BarRow]:
        sset = set(symbols) if symbols else None
        return [
            b
            for b in self._bars
            if b.bar_ts.date() == bar_date
            and b.interval == "1d"
            and (sset is None or b.symbol in sset)
        ]
