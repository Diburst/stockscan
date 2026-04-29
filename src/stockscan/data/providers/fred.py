"""FRED (Federal Reserve Economic Data) provider.

Wraps the FRED REST API. Endpoints used:
  - /series/observations  — daily-frequency observations for one series

Used by the regime detector to fetch HY OAS (``BAMLH0A0HYM2``) for the
credit-stress component of the v2 composite score. The contract is
deliberately narrower than ``DataProvider`` — FRED is a level-only data
source and doesn't speak OHLCV — so it's a standalone provider rather
than a ``DataProvider`` subclass.

Mirrors EODHDProvider's ``_get`` + ``tenacity`` retry pattern. Like that
provider, soft failure on ``get_macro_series`` is the caller's
responsibility — we surface a ``FredError`` and let the regime detector
degrade gracefully.
"""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from stockscan.config import settings
from stockscan.data.providers.base import MacroRow

log = logging.getLogger(__name__)


class FredError(RuntimeError):
    """Raised when FRED returns an error response or unexpected payload."""


class FredProvider:
    """Thin client over FRED's /series/observations endpoint."""

    name = "fred"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.api_key = api_key or settings.fred_api_key.get_secret_value()
        self.base_url = (base_url or settings.fred_base_url).rstrip("/")
        if not self.api_key:
            raise FredError("FRED_API_KEY is not set; cannot initialize FredProvider")
        # `transport` is an injection point for tests (httpx.MockTransport).
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=30.0,
            headers={"User-Agent": "stockscan/0.1"},
            transport=transport,
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
        params.setdefault("api_key", self.api_key)
        params.setdefault("file_type", "json")
        resp = self._client.get(path, params=params)
        if resp.status_code >= 500:
            # Trigger tenacity retry on 5xx via raise_for_status.
            resp.raise_for_status()
        if resp.status_code >= 400:
            raise FredError(f"FRED {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    # ------------------------------------------------------------------
    # Macro series (the only public method on this provider for v1)
    # ------------------------------------------------------------------
    def get_macro_series(
        self,
        series_code: str,
        start: date,
        end: date,
    ) -> list[MacroRow]:
        """Fetch one daily-frequency macro series in [start, end] inclusive.

        FRED's ``/series/observations`` returns a list of rows shaped like::

            {"date": "2026-04-27", "value": "3.45", ...}

        Missing observations are encoded as ``"value": "."`` — those rows
        are dropped. Everything else is parsed into a canonical
        ``MacroRow``.

        Raises ``FredError`` on HTTP failure or unexpected payload shape.
        Callers (the regime detector) are responsible for graceful
        degradation; this method does not silently return an empty list
        on failure, so detection vs. degradation stays explicit.
        """
        data = self._get(
            "/series/observations",
            series_id=series_code,
            observation_start=start.isoformat(),
            observation_end=end.isoformat(),
        )
        if not isinstance(data, dict):
            raise FredError(f"FRED: unexpected payload shape for {series_code}")
        observations = data.get("observations") or []

        out: list[MacroRow] = []
        skipped = 0
        for obs in observations:
            raw_value = obs.get("value")
            obs_date = obs.get("date")
            if raw_value is None or obs_date is None:
                skipped += 1
                continue
            # FRED uses '.' as the missing-data sentinel.
            if raw_value == ".":
                skipped += 1
                continue
            try:
                d = date.fromisoformat(obs_date)
                v = Decimal(str(raw_value))
            except (ValueError, ArithmeticError):
                # Malformed row — drop it rather than blow up the whole batch.
                skipped += 1
                continue
            out.append(
                MacroRow(
                    series_code=series_code,
                    as_of_date=d,
                    value=v,
                    source=self.name,
                )
            )
        if skipped:
            log.debug(
                "fred: %s — skipped %d row(s) (missing/malformed)",
                series_code,
                skipped,
            )
        return out

    # ------------------------------------------------------------------
    # Resource management
    # ------------------------------------------------------------------
    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> FredProvider:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
