"""EODHD (eodhistoricaldata.com) provider.

Wraps the EODHD REST API. Endpoints used:
  - /eod/{TICKER}.US                      — daily OHLCV history
  - /fundamentals/{TICKER}.US             — fundamentals (incl. earnings)
  - /calendar/earnings                    — earnings calendar by date range
  - /fundamentals/GSPC.INDX               — S&P 500 constituents (current)
  - /fundamentals/GSPC.INDX (HistoricalTickerComponents)
                                          — historical S&P 500 membership

Rate limits depend on the plan. We use httpx with tenacity retries on 5xx.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from stockscan.config import settings
from stockscan.data.providers.base import (
    BarRow,
    DataProvider,
    EarningsRow,
    UniverseMember,
)

log = logging.getLogger(__name__)

NY_TZ = ZoneInfo("America/New_York")
DAILY_CLOSE_HOUR = 16  # 16:00 ET


class EODHDError(RuntimeError):
    """Raised when EODHD returns an error response or unexpected payload."""


class EODHDProvider(DataProvider):
    name = "eodhd"

    def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
        self.api_key = api_key or settings.eodhd_api_key.get_secret_value()
        self.base_url = (base_url or settings.eodhd_base_url).rstrip("/")
        if not self.api_key:
            raise EODHDError("EODHD_API_KEY is not set; cannot initialize EODHDProvider")
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=30.0,
            headers={"User-Agent": "stockscan/0.1"},
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    @retry(
        retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
        wait=wait_exponential(multiplier=1, min=1, max=15),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    def _get(self, path: str, **params: Any) -> Any:
        params.setdefault("api_token", self.api_key)
        params.setdefault("fmt", "json")
        resp = self._client.get(path, params=params)
        if resp.status_code >= 500:
            resp.raise_for_status()
        if resp.status_code >= 400:
            raise EODHDError(f"EODHD {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    @staticmethod
    def _to_decimal(value: Any) -> Decimal:
        if value is None:
            return Decimal(0)
        return Decimal(str(value))

    # ------------------------------------------------------------------
    # Bars
    # ------------------------------------------------------------------
    def get_bars(
        self,
        symbol: str,
        start: date,
        end: date,
        interval: str = "1d",
    ) -> list[BarRow]:
        if interval != "1d":
            raise NotImplementedError(
                "EODHDProvider currently supports only daily bars. "
                "Intraday endpoint integration is a Phase 1.5 follow-up."
            )

        path = f"/eod/{symbol}.US"
        rows = self._get(
            path,
            from_=start.isoformat(),
            to=end.isoformat(),
            period="d",
        )
        # EODHD returns: [{date, open, high, low, close, adjusted_close, volume}, ...]
        out: list[BarRow] = []
        for r in rows or []:
            d = date.fromisoformat(r["date"])
            ts = datetime(d.year, d.month, d.day, DAILY_CLOSE_HOUR, tzinfo=NY_TZ)
            out.append(
                BarRow(
                    symbol=symbol,
                    bar_ts=ts.astimezone(timezone.utc),
                    interval="1d",
                    open=self._to_decimal(r.get("open")),
                    high=self._to_decimal(r.get("high")),
                    low=self._to_decimal(r.get("low")),
                    close=self._to_decimal(r.get("close")),
                    adj_close=self._to_decimal(r.get("adjusted_close")),
                    volume=int(r.get("volume") or 0),
                    source=self.name,
                )
            )
        return out

    # ------------------------------------------------------------------
    # Universe (S&P 500)
    # ------------------------------------------------------------------
    def get_sp500_constituents(self) -> list[UniverseMember]:
        data = self._get("/fundamentals/GSPC.INDX")
        components = (data or {}).get("Components") or {}
        today = date.today()
        out: list[UniverseMember] = []
        for entry in components.values():
            symbol = (entry or {}).get("Code")
            if not symbol:
                continue
            out.append(UniverseMember(symbol=symbol, joined_date=today, left_date=None))
        return out

    def get_sp500_historical_constituents(self) -> list[UniverseMember]:
        data = self._get("/fundamentals/GSPC.INDX")
        history = (data or {}).get("HistoricalTickerComponents") or {}
        out: list[UniverseMember] = []
        for entry in history.values():
            symbol = (entry or {}).get("Code")
            joined = (entry or {}).get("StartDate")
            left = (entry or {}).get("EndDate")
            if not symbol or not joined:
                continue
            try:
                joined_d = date.fromisoformat(joined)
            except ValueError:
                continue
            left_d: date | None
            if left:
                try:
                    left_d = date.fromisoformat(left)
                except ValueError:
                    left_d = None
            else:
                left_d = None
            out.append(UniverseMember(symbol=symbol, joined_date=joined_d, left_date=left_d))
        return out

    # ------------------------------------------------------------------
    # Earnings calendar
    # ------------------------------------------------------------------
    def get_earnings(
        self,
        symbols: list[str],
        start: date,
        end: date,
    ) -> list[EarningsRow]:
        # EODHD's earnings endpoint accepts comma-separated symbols.
        # If the list is large, batch in groups of 100 to keep URLs sane.
        out: list[EarningsRow] = []
        for batch_start in range(0, len(symbols), 100):
            batch = symbols[batch_start : batch_start + 100]
            data = self._get(
                "/calendar/earnings",
                symbols=",".join(f"{s}.US" for s in batch),
                from_=start.isoformat(),
                to=end.isoformat(),
            )
            for r in (data or {}).get("earnings", []):
                code = (r.get("code") or "").split(".")[0]
                if not code:
                    continue
                report = r.get("report_date") or r.get("date")
                if not report:
                    continue
                try:
                    report_d = date.fromisoformat(report)
                except ValueError:
                    continue
                tod = (r.get("before_after_market") or "unknown").lower()
                if tod not in {"bmo", "amc", "unknown"}:
                    tod = "unknown"
                out.append(
                    EarningsRow(
                        symbol=code,
                        report_date=report_d,
                        time_of_day=tod,
                        estimate=self._to_decimal(r.get("estimate"))
                        if r.get("estimate") is not None
                        else None,
                        actual=self._to_decimal(r.get("actual"))
                        if r.get("actual") is not None
                        else None,
                    )
                )
        return out

    # ------------------------------------------------------------------
    # Fundamentals (full payload — heavy; cache locally)
    # ------------------------------------------------------------------
    def get_fundamentals(self, symbol: str) -> dict[str, Any] | None:
        """One API call → full fundamentals payload (~hundreds of KB).

        Endpoint: /fundamentals/{TICKER}.US?fmt=json
        The response is a deeply nested object with sections for General,
        Highlights, Valuation, SharesStats, Technicals, Earnings,
        Financials, etc. We hand it back as-is; the store layer extracts
        specific fields.
        """
        path = f"/fundamentals/{symbol}.US"
        try:
            data = self._get(path)
        except EODHDError as exc:
            log.warning("fundamentals unavailable for %s: %s", symbol, exc)
            return None
        if not isinstance(data, dict) or not data:
            return None
        return data

    # ------------------------------------------------------------------
    # Bulk EOD — DESIGN §4.1 daily refresh path
    # ------------------------------------------------------------------
    def get_eod_bulk(
        self,
        bar_date: date,
        exchange: str = "US",
        symbols: list[str] | None = None,
    ) -> list[BarRow]:
        """One API call → all-symbol EOD for a single trading day.

        Endpoint: /eod-bulk-last-day/{exchange}?date=YYYY-MM-DD&fmt=json
        Optional `symbols` filter narrows the response server-side.

        Returns BarRow entries for every symbol returned by the provider
        (or the requested subset). If the date isn't a trading day the
        server typically returns the previous one — we trust the response
        timestamps and store whatever they give us.
        """
        path = f"/eod-bulk-last-day/{exchange}"
        params: dict[str, Any] = {"date": bar_date.isoformat()}
        if symbols:
            # EODHD wants comma-separated tickers WITHOUT the .US suffix here.
            params["symbols"] = ",".join(symbols)
        rows = self._get(path, **params)

        out: list[BarRow] = []
        for r in rows or []:
            code = r.get("code") or r.get("Code")
            row_date_str = r.get("date") or r.get("Date")
            if not code or not row_date_str:
                continue
            try:
                d = date.fromisoformat(row_date_str)
            except ValueError:
                continue
            ts = datetime(d.year, d.month, d.day, DAILY_CLOSE_HOUR, tzinfo=NY_TZ)
            out.append(
                BarRow(
                    symbol=code,
                    bar_ts=ts.astimezone(timezone.utc),
                    interval="1d",
                    open=self._to_decimal(r.get("open")),
                    high=self._to_decimal(r.get("high")),
                    low=self._to_decimal(r.get("low")),
                    close=self._to_decimal(r.get("close")),
                    adj_close=self._to_decimal(r.get("adjusted_close")),
                    volume=int(r.get("volume") or 0),
                    source=self.name,
                )
            )
        return out

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> EODHDProvider:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
