"""SuggestionBroker — basic shape tests.

Full DB-backed tests are in test_integration_*.py and require a live
TimescaleDB instance.
"""

from stockscan.broker import SuggestionBroker
from stockscan.broker.base import OrderStatus


def test_suggestion_broker_is_authenticated() -> None:
    b = SuggestionBroker()
    assert b.is_authenticated() is True
    assert b.name == "suggestion"


def test_get_orders_returns_empty_in_phase0() -> None:
    b = SuggestionBroker()
    assert b.get_orders() == []
    assert b.get_orders(status=OrderStatus.SUGGESTED) == []
