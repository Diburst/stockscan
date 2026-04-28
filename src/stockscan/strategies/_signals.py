"""Shared dataclasses for the strategy contract.

Kept in a leaf module so concrete strategies can import these without
importing the base ABC machinery (avoids circular imports).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

Side = Literal["long", "short"]


@dataclass(frozen=True, slots=True)
class RawSignal:
    """A pre-filtering signal emitted by Strategy.signals().

    The scanner runs the filter chain over these and persists either
    a passing 'new' signal or a 'rejected' signal with reason.
    """

    strategy_name: str
    strategy_version: str
    symbol: str
    side: Side
    score: Decimal
    suggested_entry: Decimal
    suggested_stop: Decimal
    suggested_target: Decimal | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PositionSnapshot:
    """Minimal position view passed to Strategy.exit_rules()."""

    symbol: str
    qty: int
    avg_cost: Decimal
    opened_at: datetime
    strategy: str


@dataclass(frozen=True, slots=True)
class ExitDecision:
    """Sell decision returned by Strategy.exit_rules()."""

    reason: str  # human-readable: 'rsi_cross', 'time_stop', 'hard_stop', etc.
    qty: int  # full or partial
    order_type: str = "market_on_open"
    limit_price: Decimal | None = None
