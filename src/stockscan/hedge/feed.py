"""Real-time price feeds for the delta-hedge daemon.

Two implementations behind one tiny async interface:

  * :class:`EodhdWebsocketFeed` — the production feed. Opens a single EODHD
    WebSocket (``wss://ws.eodhistoricaldata.com/ws/us``), subscribes the union
    of symbols across all active hedges (EODHD allows 50 per connection — plenty
    for a personal book), and yields a :class:`PriceTick` per trade print. The
    ``websockets`` package is imported lazily so the rest of the app — and the
    test suite — never depends on it.

  * :class:`SimulatedFeed` — a deterministic-ish GBM random walk with no network
    and no dependencies. It's what runs in the sandbox, in unit tests, and when
    you don't hold an EODHD real-time subscription. Same interface, so the daemon
    can't tell the difference.

Interface
---------
    feed = SomeFeed(...)
    await feed.set_symbols({"MU", "AAPL"})
    async for tick in feed.stream():
        ...                     # tick.symbol, tick.price, tick.ts
    await feed.close()

``set_symbols`` can be called at any time (the daemon calls it when a hedge is
opened, paused, or closed); the feed reconciles its subscriptions on the fly.
"""

from __future__ import annotations

import asyncio
import logging
import random
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime

log = logging.getLogger(__name__)

_EODHD_US_WS = "wss://ws.eodhistoricaldata.com/ws/us"


@dataclass(frozen=True, slots=True)
class PriceTick:
    symbol: str
    price: float
    ts: datetime


class PriceFeed(ABC):
    """A live source of :class:`PriceTick` for a mutable set of symbols."""

    @abstractmethod
    async def set_symbols(self, symbols: set[str]) -> None:
        """Reconcile the subscribed universe to exactly ``symbols``."""

    @abstractmethod
    def stream(self) -> AsyncIterator[PriceTick]:
        """Yield ticks until :meth:`close` is called."""

    @abstractmethod
    async def close(self) -> None:
        ...


class SimulatedFeed(PriceFeed):
    """Dependency-free GBM walk — for tests, the sandbox, and no-sub dev.

    Each symbol drifts as ``S ← S · exp(σ·√dt·Z)`` on a fixed cadence. Seed
    prices come from the caller (the daemon passes the last bar close); unknown
    symbols start at ``default_price``.
    """

    def __init__(
        self,
        *,
        seed_prices: dict[str, float] | None = None,
        interval_s: float = 1.0,
        annual_vol: float = 0.30,
        default_price: float = 100.0,
        rng: random.Random | None = None,
    ) -> None:
        self._prices: dict[str, float] = dict(seed_prices or {})
        self._symbols: set[str] = set(self._prices)
        self._interval = interval_s
        self._vol = annual_vol
        self._default = default_price
        self._rng = rng or random.Random()
        self._closed = False

    async def set_symbols(self, symbols: set[str]) -> None:
        self._symbols = set(symbols)
        for sym in self._symbols:
            self._prices.setdefault(sym, self._default)

    def seed(self, symbol: str, price: float) -> None:
        """Set a symbol's starting price (used by the daemon at open time)."""
        if price > 0:
            self._prices[symbol] = price

    async def stream(self) -> AsyncIterator[PriceTick]:
        # √dt for the per-tick step, dt = interval / seconds-in-a-trading-year.
        dt = self._interval / (252.0 * 6.5 * 3600.0)
        step = self._vol * (dt ** 0.5)
        while not self._closed:
            await asyncio.sleep(self._interval)
            for sym in list(self._symbols):
                s = self._prices.get(sym, self._default)
                z = self._rng.gauss(0.0, 1.0)
                s = max(0.01, s * (2.718281828459045 ** (step * z)))
                self._prices[sym] = s
                yield PriceTick(sym, round(s, 4), datetime.now(UTC))

    async def close(self) -> None:
        self._closed = True


class EodhdWebsocketFeed(PriceFeed):
    """Production feed over the EODHD US real-time WebSocket.

    Requires an EODHD real-time entitlement (All-In-One / All-World Extended).
    Reconnects with backoff on drop; re-subscribes the current symbol set on
    every (re)connect. ``websockets`` is imported lazily inside :meth:`stream`.
    """

    def __init__(self, api_token: str, *, url: str = _EODHD_US_WS, max_backoff_s: float = 30.0) -> None:
        if not api_token:
            raise ValueError("EODHD API token required for the live feed")
        self._token = api_token
        self._url = f"{url}?api_token={api_token}"
        self._symbols: set[str] = set()
        self._ws = None  # set once connected
        self._closed = False
        self._max_backoff = max_backoff_s

    async def set_symbols(self, symbols: set[str]) -> None:
        new = set(symbols)
        added, removed = new - self._symbols, self._symbols - new
        self._symbols = new
        if self._ws is not None:
            if added:
                await self._send("subscribe", added)
            if removed:
                await self._send("unsubscribe", removed)

    async def _send(self, action: str, symbols: set[str]) -> None:
        import json

        if self._ws is None or not symbols:
            return
        await self._ws.send(json.dumps({"action": action, "symbols": ",".join(sorted(symbols))}))

    async def stream(self) -> AsyncIterator[PriceTick]:
        import websockets  # lazy: only the live path needs it.

        backoff = 1.0
        while not self._closed:
            try:
                async with websockets.connect(self._url) as ws:
                    self._ws = ws
                    backoff = 1.0
                    if self._symbols:
                        await self._send("subscribe", self._symbols)
                    async for raw in ws:
                        tick = _parse_eodhd_message(raw)
                        if tick is not None:
                            yield tick
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect on anything
                if self._closed:
                    break
                log.warning("EODHD feed dropped (%s); reconnecting in %.0fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(self._max_backoff, backoff * 2.0)
            finally:
                self._ws = None

    async def close(self) -> None:
        self._closed = True
        if self._ws is not None:
            await self._ws.close()


def _parse_eodhd_message(raw: str | bytes) -> PriceTick | None:
    """Parse one EODHD US trade message into a :class:`PriceTick`.

    Trade messages carry ``s`` (symbol), ``p`` (price), ``t`` (ms epoch).
    Status/heartbeat frames (no ``p``) return ``None``.
    """
    import json

    try:
        msg = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(msg, dict):
        return None
    sym = msg.get("s")
    price = msg.get("p")
    if sym is None or price is None:
        return None
    try:
        price_f = float(price)
    except (ValueError, TypeError):
        return None
    if price_f <= 0:
        return None
    ts_ms = msg.get("t")
    try:
        ts = datetime.fromtimestamp(float(ts_ms) / 1000.0, tz=UTC) if ts_ms else datetime.now(UTC)
    except (ValueError, TypeError, OSError):
        ts = datetime.now(UTC)
    return PriceTick(str(sym), price_f, ts)
