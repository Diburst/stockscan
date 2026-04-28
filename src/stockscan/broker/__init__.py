"""Broker abstraction (DESIGN §4.6)."""

from stockscan.broker.base import (
    Account,
    Broker,
    BrokerOrder,
    OrderRequest,
    OrderStatus,
    Quote,
)
from stockscan.broker.paper import PaperBroker
from stockscan.broker.suggestion import SuggestionBroker

__all__ = [
    "Account",
    "Broker",
    "BrokerOrder",
    "OrderRequest",
    "OrderStatus",
    "Quote",
    "PaperBroker",
    "SuggestionBroker",
]
