"""Position sizer (DESIGN §4.7).

Default rule: risk a fixed % of equity per trade, with stop distance
determining share count. Integer shares only (E*TRADE constraint).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal


@dataclass(frozen=True, slots=True)
class SizingResult:
    qty: int
    risk_dollars: Decimal
    notional: Decimal
    rejected_reason: str | None = None


def position_size(
    equity: Decimal,
    entry_price: Decimal,
    stop_price: Decimal,
    risk_pct: Decimal,
    *,
    max_position_pct: Decimal | None = None,
) -> SizingResult:
    """Compute integer share count for a long entry.

    qty = floor((equity * risk_pct) / (entry - stop))

    If max_position_pct is given, qty is also capped so notional <=
    equity * max_position_pct.
    """
    if entry_price <= 0:
        return SizingResult(0, Decimal(0), Decimal(0), "invalid_entry_price")
    if stop_price >= entry_price:
        return SizingResult(0, Decimal(0), Decimal(0), "stop_above_entry")
    if risk_pct <= 0:
        return SizingResult(0, Decimal(0), Decimal(0), "invalid_risk_pct")
    if equity <= 0:
        return SizingResult(0, Decimal(0), Decimal(0), "no_equity")

    risk_dollars = (equity * risk_pct).quantize(Decimal("0.01"))
    per_share_risk = entry_price - stop_price
    raw_qty = (risk_dollars / per_share_risk).to_integral_value(rounding=ROUND_DOWN)
    qty = max(0, int(raw_qty))

    if max_position_pct is not None and qty > 0:
        max_notional = equity * max_position_pct
        max_qty = int((max_notional / entry_price).to_integral_value(rounding=ROUND_DOWN))
        if max_qty < qty:
            qty = max_qty

    if qty <= 0:
        return SizingResult(0, risk_dollars, Decimal(0), "qty_zero")

    notional = (entry_price * qty).quantize(Decimal("0.01"))
    return SizingResult(qty=qty, risk_dollars=risk_dollars, notional=notional)
