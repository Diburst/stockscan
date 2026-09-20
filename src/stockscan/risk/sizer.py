"""Position sizer (DESIGN §4.7).

Two sizing rules, chosen by whether the signal carries a stop:

* **Stop-based** — risk a fixed percent of equity per trade; the stop
  distance sets the share count. Momentum uses this.
* **Fixed fraction** — a fixed percent of equity per position, for
  strategies that deliberately trade without a price stop (mean reversion,
  where stops cut returns more than drawdown — Kaminski & Lo 2014; Alvarez).

Both are capped so notional never exceeds ``max_position_pct`` of equity.
Integer shares only (E*TRADE constraint).
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
    *,
    stop_price: Decimal | None,
    risk_pct: Decimal,
    position_pct: Decimal | None,
    max_position_pct: Decimal,
) -> SizingResult:
    """Integer share count for a long entry.

    With a stop: ``qty = floor(equity × risk_pct / (entry − stop))``.
    Without one: ``qty = floor(equity × position_pct / entry)``, and
    ``risk_dollars`` reports the full notional (the position is the risk).
    Either way ``notional ≤ equity × max_position_pct``.
    """
    if entry_price <= 0:
        return SizingResult(0, Decimal(0), Decimal(0), "invalid_entry_price")
    if equity <= 0:
        return SizingResult(0, Decimal(0), Decimal(0), "no_equity")

    if stop_price is not None:
        if stop_price >= entry_price:
            return SizingResult(0, Decimal(0), Decimal(0), "stop_above_entry")
        if risk_pct <= 0:
            return SizingResult(0, Decimal(0), Decimal(0), "invalid_risk_pct")
        risk_dollars = (equity * risk_pct).quantize(Decimal("0.01"))
        raw_qty = (risk_dollars / (entry_price - stop_price)).to_integral_value(rounding=ROUND_DOWN)
    else:
        if position_pct is None or position_pct <= 0:
            return SizingResult(0, Decimal(0), Decimal(0), "no_stop_and_no_position_pct")
        risk_dollars = (equity * position_pct).quantize(Decimal("0.01"))
        raw_qty = (risk_dollars / entry_price).to_integral_value(rounding=ROUND_DOWN)

    qty = max(0, int(raw_qty))
    max_qty = int((equity * max_position_pct / entry_price).to_integral_value(rounding=ROUND_DOWN))
    qty = min(qty, max_qty)
    if qty <= 0:
        return SizingResult(0, risk_dollars, Decimal(0), "qty_zero")
    return SizingResult(qty=qty, risk_dollars=risk_dollars, notional=(entry_price * qty).quantize(Decimal("0.01")))


def size_for_strategy(
    strategy_cls: type,
    equity: Decimal,
    entry_price: Decimal,
    stop_price: Decimal | None,
    *,
    vol_scalar: float,
    max_position_pct: Decimal,
) -> SizingResult:
    """Size a signal the way the strategy declares, then apply the regime
    layer's vol scalar if the strategy opts in.

    Shared by the live runner and the backtest engine so a backtest measures
    exactly the sizing that trades live.
    """
    base = position_size(
        equity,
        entry_price,
        stop_price=stop_price,
        risk_pct=Decimal(str(strategy_cls.default_risk_pct)),
        position_pct=(
            Decimal(str(strategy_cls.position_pct))
            if strategy_cls.position_pct is not None
            else None
        ),
        max_position_pct=max_position_pct,
    )
    if base.qty <= 0 or not strategy_cls.sizes_down_in_high_vol or vol_scalar >= 1.0:
        return base
    qty = int(Decimal(base.qty) * Decimal(str(vol_scalar)))
    if qty <= 0:
        return SizingResult(0, base.risk_dollars, Decimal(0), "vol_scalar_zero_size")
    return SizingResult(
        qty=qty,
        risk_dollars=(base.risk_dollars * Decimal(str(vol_scalar))).quantize(Decimal("0.01")),
        notional=(entry_price * qty).quantize(Decimal("0.01")),
    )
