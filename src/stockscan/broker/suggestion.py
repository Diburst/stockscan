"""SuggestionBroker — the no-execution broker.

Default broker in v1, and the auto-fallback whenever E*TRADE auth lapses.
`place_order` does NOT transmit an order anywhere — it persists the intent
to the `suggestions` table and surfaces it in the UI / notifications.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import text

from stockscan.broker.base import (
    Account,
    Broker,
    BrokerOrder,
    BrokerPosition,
    OrderRequest,
    OrderStatus,
    Quote,
)
from stockscan.db import session_scope

log = logging.getLogger(__name__)


class SuggestionBroker(Broker):
    """No-execution broker. Records intent; never transmits."""

    name = "suggestion"

    def __init__(self, account_id: int = 1) -> None:
        self.account_id = account_id

    def is_authenticated(self) -> bool:
        return True  # always available

    def get_account(self) -> Account:
        # Read from accounts + equity_history for live equity.
        sql = text(
            """
            SELECT
                COALESCE((SELECT total_equity FROM equity_history
                          WHERE account_id = :aid
                          ORDER BY as_of_date DESC LIMIT 1), 0) AS equity,
                COALESCE((SELECT cash FROM equity_history
                          WHERE account_id = :aid
                          ORDER BY as_of_date DESC LIMIT 1), 0) AS cash
            """
        )
        with session_scope() as s:
            row = s.execute(sql, {"aid": self.account_id}).one()
        equity = Decimal(str(row.equity))
        cash = Decimal(str(row.cash))
        return Account(
            account_id=str(self.account_id),
            cash=cash,
            equity=equity,
            buying_power=cash,
        )

    def get_positions(self) -> list[BrokerPosition]:
        sql = text(
            """
            SELECT symbol, SUM(qty_remaining) AS qty,
                   SUM(qty_remaining * cost_basis) / NULLIF(SUM(qty_remaining), 0) AS avg_cost
            FROM tax_lots
            WHERE account_id = :aid AND qty_remaining > 0
            GROUP BY symbol
            """
        )
        with session_scope() as s:
            rows = s.execute(sql, {"aid": self.account_id}).all()
        return [
            BrokerPosition(
                symbol=r.symbol,
                qty=int(r.qty),
                avg_cost=Decimal(str(r.avg_cost)),
                market_value=Decimal(0),  # filled in by mark-to-market layer
            )
            for r in rows
        ]

    def get_orders(self, status: OrderStatus | None = None) -> list[BrokerOrder]:
        # SuggestionBroker doesn't transmit orders, so this returns an empty
        # list for now. The UI reads from the `suggestions` and `orders`
        # tables directly to render today's ideas.
        return []

    def place_order(self, order: OrderRequest) -> BrokerOrder:
        """Persist as a suggestion, return a SUGGESTED-status BrokerOrder."""
        suggestion_id = self._record_suggestion(order)
        log.info(
            "SuggestionBroker.place_order: recorded suggestion #%d (%s %d %s @ %s)",
            suggestion_id,
            order.side,
            order.qty,
            order.symbol,
            order.order_type,
        )
        return BrokerOrder(
            broker_order_id=f"sug-{suggestion_id}",
            symbol=order.symbol,
            side=order.side,
            qty=order.qty,
            order_type=order.order_type,
            status=OrderStatus.SUGGESTED,
            limit_price=order.limit_price,
            stop_price=order.stop_price,
            submitted_at=datetime.now(timezone.utc),
            filled_at=None,
            avg_fill_price=None,
            commission=Decimal(0),
        )

    def cancel_order(self, broker_order_id: str) -> None:
        # Mark the suggestion as skipped if it's a 'sug-NNN' id.
        if not broker_order_id.startswith("sug-"):
            return
        sid = int(broker_order_id.removeprefix("sug-"))
        with session_scope() as s:
            s.execute(
                text(
                    "UPDATE suggestions SET user_action='skipped', user_action_at=NOW() "
                    "WHERE suggestion_id=:sid"
                ),
                {"sid": sid},
            )

    def get_quote(self, symbol: str) -> Quote:
        # Use the latest stored bar's close as a proxy quote.
        sql = text(
            """
            SELECT close, bar_ts FROM bars
            WHERE symbol = :symbol AND interval='1d'
            ORDER BY bar_ts DESC LIMIT 1
            """
        )
        with session_scope() as s:
            row = s.execute(sql, {"symbol": symbol}).first()
        if not row:
            raise LookupError(f"No bars for {symbol}; cannot quote")
        close = Decimal(str(row.close))
        return Quote(symbol=symbol, bid=close, ask=close, last=close, timestamp=row.bar_ts)

    # -------------- private --------------
    def _record_suggestion(self, order: OrderRequest) -> int:
        sql = text(
            """
            INSERT INTO suggestions (account_id, signal_id, action, qty, journal_notes)
            VALUES (:aid, NULL, :action, :qty, :note)
            RETURNING suggestion_id;
            """
        )
        action = f"{order.side} {order.symbol} {order.order_type}"
        if order.limit_price:
            action += f" limit {order.limit_price}"
        if order.stop_price:
            action += f" stop {order.stop_price}"
        ref = order.client_ref or f"auto-{uuid4().hex[:8]}"
        note = f"client_ref={ref}"
        with session_scope() as s:
            row = s.execute(
                sql, {"aid": self.account_id, "action": action, "qty": order.qty, "note": note}
            ).one()
        return int(row.suggestion_id)
