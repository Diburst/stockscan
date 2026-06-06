"""Unit tests for the InsiderNetBuys aggregation properties.

The net-count and net-value math are what the watchlist pill and the
analysis card both render — they need to behave correctly across the
edge cases (all buys, all sells, no activity, etc.).
"""

from __future__ import annotations

from stockscan.insider.store import InsiderNetBuys


def test_net_count_buys_outnumber_sales() -> None:
    n = InsiderNetBuys(buy_count=5, sell_count=2, buy_value=500_000.0, sell_value=100_000.0)
    assert n.net_count == 3
    assert n.net_value == 400_000.0
    assert n.has_activity


def test_net_count_sales_outnumber_buys() -> None:
    n = InsiderNetBuys(buy_count=1, sell_count=4, buy_value=50_000.0, sell_value=400_000.0)
    assert n.net_count == -3
    assert n.net_value == -350_000.0
    assert n.has_activity


def test_no_activity_signals_correctly() -> None:
    n = InsiderNetBuys(buy_count=0, sell_count=0, buy_value=0.0, sell_value=0.0)
    assert not n.has_activity
    assert n.net_count == 0
    assert n.net_value == 0.0


def test_only_buys() -> None:
    n = InsiderNetBuys(buy_count=3, sell_count=0, buy_value=200_000.0, sell_value=0.0)
    assert n.net_count == 3
    assert n.net_value == 200_000.0
    assert n.has_activity


def test_only_sells() -> None:
    n = InsiderNetBuys(buy_count=0, sell_count=5, buy_value=0.0, sell_value=750_000.0)
    assert n.net_count == -5
    assert n.net_value == -750_000.0
    assert n.has_activity
