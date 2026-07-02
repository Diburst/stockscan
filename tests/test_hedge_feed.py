"""Feed parsing + the dependency-free simulated feed."""

from __future__ import annotations

import asyncio

from stockscan.hedge.feed import SimulatedFeed, _parse_eodhd_message


def test_parse_eodhd_trade_message():
    tick = _parse_eodhd_message('{"s":"MU","p":123.45,"t":1751000000000}')
    assert tick is not None
    assert tick.symbol == "MU"
    assert tick.price == 123.45


def test_parse_eodhd_ignores_status_and_garbage():
    assert _parse_eodhd_message('{"status":"ok"}') is None  # no price
    assert _parse_eodhd_message("not json") is None
    assert _parse_eodhd_message('{"s":"MU","p":-1}') is None  # non-positive price


def test_parse_eodhd_missing_timestamp_still_ticks():
    tick = _parse_eodhd_message('{"s":"AAPL","p":200}')
    assert tick is not None and tick.price == 200.0


def test_simulated_feed_seeds_and_emits_ticks():
    async def _drive() -> list:
        feed = SimulatedFeed(interval_s=0.01, seed_prices={"MU": 1300.0})
        await feed.set_symbols({"MU"})
        got = []
        agen = feed.stream()
        try:
            for _ in range(3):
                got.append(await asyncio.wait_for(agen.__anext__(), timeout=2.0))
        finally:
            await feed.close()
            await agen.aclose()
        return got

    ticks = asyncio.run(_drive())
    assert len(ticks) == 3
    assert all(t.symbol == "MU" and t.price > 0 for t in ticks)
    # Starts near the seed (one small GBM step).
    assert 1200.0 < ticks[0].price < 1400.0
