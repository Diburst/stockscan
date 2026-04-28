"""PaperBroker — in-process simulated broker for the backtester.

Simulates fills against historical bars. Used by both the backtester
(Phase 1) and any future "dry run" mode of the live engine.

In Phase 0 we ship a minimal implementation; the realistic fill model
(slippage, partial fills, market_on_open semantics) lands in Phase 1
when we wire up the backtester.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from itertools import count

from stockscan.broker.base import (
    Account,
    Broker,
    BrokerOrder,
    BrokerPosition,
    OrderRequest,
    OrderStatus,
    Quote,
)


class PaperBroker(Broker):
    name = "paper"

    def __init__(self, starting_cash: Decimal = Decimal("1000000")) -> None:
        self._cash = starting_cash
        self._equity = starting_cash
        self._positions: dict[str, BrokerPosition] = {}
        self._orders: dict[str, BrokerOrder] = {}
        self._id_seq = count(1)
        self._slippage_bps = Decimal("5")  # 5 bps default; Phase 1 makes this configurable

    def is_authenticated(self) -> bool:
        return True

    def get_account(self) -> Account:
        return Account(
            account_id="paper",
            cash=self._cash,
            equity=self._equity,
            buying_power=self._cash,
        )

    def get_positions(self) -> list[BrokerPosition]:
        return list(self._positions.values())

    def get_orders(self, status: OrderStatus | None = None) -> list[BrokerOrder]:
        if status is None:
            return list(self._orders.values())
        return [o for o in self._orders.values() if o.status == status]

    def place_order(self, order: OrderRequest) -> BrokerOrder:
        """Phase 0 stub: accept the order and immediately mark FILLED at the
        suggested limit (or 0 if unspecified). Phase 1 replaces this with a
        bar-driven fill simulator."""
        oid = f"paper-{next(self._id_seq)}"
        fill = order.limit_price or order.stop_price or Decimal(0)
        if order.order_type == "market" and fill == 0:
            fill = Decimal(0)  # caller must set context for paper market fills

        broker_order = BrokerOrder(
            broker_order_id=oid,
            symbol=order.symbol,
            side=order.side,
            qty=order.qty,
            order_type=order.order_type,
            status=OrderStatus.FILLED if fill > 0 else OrderStatus.SUBMITTED,
            limit_price=order.limit_price,
            stop_price=order.stop_price,
            submitted_at=datetime.now(timezone.utc),
            filled_at=datetime.now(timezone.utc) if fill > 0 else None,
            avg_fill_price=fill if fill > 0 else None,
            commission=Decimal(0),
        )
        self._orders[oid] = broker_order
        return broker_order

    def cancel_order(self, broker_order_id: str) -> None:
        if broker_order_id in self._orders:
            old = self._orders[broker_order_id]
            self._orders[broker_order_id] = BrokerOrder(
                broker_order_id=old.broker_order_id,
                symbol=old.symbol,
                side=old.side,
                qty=old.qty,
                order_type=old.order_type,
                status=OrderStatus.CANCELED,
                limit_price=old.limit_price,
                stop_price=old.stop_price,
                submitted_at=old.submitted_at,
                filled_at=None,
                avg_fill_price=None,
                commission=old.commission,
            )

    def get_quote(self, symbol: str) -> Quote:
        # PaperBroker doesn't have its own market data; backtester provides it.
        raise NotImplementedError(
            "PaperBroker.get_quote is provided by the backtester loop in Phase 1."
        )
