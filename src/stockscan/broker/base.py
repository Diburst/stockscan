"""Broker contract (DESIGN §4.6).

All broker integrations (E*TRADE, Alpaca, Paper, Suggestion) implement
this. Application code never imports a concrete broker — it gets one
from a factory based on config + auth state.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Literal


class OrderStatus(str, Enum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"
    SUGGESTED = "suggested"  # SuggestionBroker only


OrderType = Literal["market", "limit", "stop", "stop_limit", "market_on_open"]
TimeInForce = Literal["day", "gtc", "ioc", "fok"]
Side = Literal["buy", "sell"]


@dataclass(frozen=True, slots=True)
class OrderRequest:
    """Outbound order. The broker translates this to its own API shape."""

    symbol: str
    side: Side
    qty: int
    order_type: OrderType
    time_in_force: TimeInForce = "day"
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    client_ref: str | None = None  # idempotency token for retries


@dataclass(frozen=True, slots=True)
class BrokerOrder:
    """An order known to the broker (after submission)."""

    broker_order_id: str
    symbol: str
    side: Side
    qty: int
    order_type: OrderType
    status: OrderStatus
    limit_price: Decimal | None
    stop_price: Decimal | None
    submitted_at: datetime | None
    filled_at: datetime | None
    avg_fill_price: Decimal | None
    commission: Decimal


@dataclass(frozen=True, slots=True)
class Account:
    account_id: str
    cash: Decimal
    equity: Decimal
    buying_power: Decimal


@dataclass(frozen=True, slots=True)
class BrokerPosition:
    symbol: str
    qty: int
    avg_cost: Decimal
    market_value: Decimal


@dataclass(frozen=True, slots=True)
class Quote:
    symbol: str
    bid: Decimal
    ask: Decimal
    last: Decimal
    timestamp: datetime


class Broker(ABC):
    """Broker contract. Concrete implementations: ETradeBroker, AlpacaBroker,
    PaperBroker, SuggestionBroker."""

    name: str

    @abstractmethod
    def get_account(self) -> Account:
        """Cash, equity, buying power."""

    @abstractmethod
    def get_positions(self) -> list[BrokerPosition]:
        """Current positions at the broker."""

    @abstractmethod
    def get_orders(self, status: OrderStatus | None = None) -> list[BrokerOrder]:
        """Recent orders, optionally filtered by status."""

    @abstractmethod
    def place_order(self, order: OrderRequest) -> BrokerOrder:
        """Submit `order`. Idempotent if `client_ref` is set."""

    @abstractmethod
    def cancel_order(self, broker_order_id: str) -> None: ...

    @abstractmethod
    def get_quote(self, symbol: str) -> Quote: ...

    @abstractmethod
    def is_authenticated(self) -> bool:
        """True if the broker can place orders right now."""
