"""Pure P&L + settlement math for a delta-hedge position.

No DB, no I/O — floats in, floats out — so every number the page shows can be
unit-tested in isolation. The daemon and the store call these; they own
persistence.

The three things a hedge position tracks
----------------------------------------
  1. **The option leg** — priced against Black-Scholes at the live spot. For a
     short option the P&L is ``premium − mark``; for a long option it's
     ``mark − premium`` (``premium`` is stored as a positive magnitude).
  2. **Realised hedge P&L** — locked-in gains/losses from stock we've already
     traded out of, using signed average-cost accounting (:func:`apply_fill`).
  3. **Unrealised hedge P&L** — mark-to-market on the shares still held.

``net_open_pnl`` sums all three: the live answer to "is this trade winning?"
For a short-gamma hedge the stock leg bleeds by design and the premium is what
pays for it, so seeing the three components side by side is the whole point.

Sign conventions
----------------
  * ``held_shares`` is **signed**: positive = long stock, negative = short stock.
  * ``premium`` and ``multiplier`` are positive magnitudes; ``contracts`` > 0.
  * A "fill" of +q means we bought q shares, −q means we sold q shares.
"""

from __future__ import annotations

from dataclasses import dataclass

from stockscan.analysis import black_scholes


@dataclass(frozen=True, slots=True)
class FillResult:
    """New position state after applying one stock fill, plus P&L it realised."""

    held_shares: int
    avg_cost: float
    realized_pnl: float  # cumulative realised (input realized + this fill's)
    realized_delta: float  # just what THIS fill locked in


def apply_fill(
    *,
    held_shares: int,
    avg_cost: float,
    realized_pnl: float,
    fill_qty: int,
    fill_price: float,
) -> FillResult:
    """Apply a signed stock fill with average-cost accounting.

    Handles adding to a position, trimming it, and flipping through zero:

      * **Increasing** magnitude (or opening from flat) → blend into ``avg_cost``.
      * **Reducing** → realise ``(fill_price − avg_cost) × closed`` on the closed
        shares (signed by the side of the existing position); ``avg_cost`` is
        unchanged on the remainder.
      * **Flipping** past zero → realise the whole old position, then open the
        residual at ``fill_price``.
    """
    if fill_qty == 0:
        return FillResult(held_shares, avg_cost, realized_pnl, 0.0)

    new_held = held_shares + fill_qty
    realized_delta = 0.0

    same_direction = (held_shares == 0) or (held_shares > 0) == (fill_qty > 0)

    if same_direction:
        # Adding to (or opening) the position → weighted-average the cost.
        total_cost = avg_cost * abs(held_shares) + fill_price * abs(fill_qty)
        avg_cost = total_cost / abs(new_held) if new_held != 0 else 0.0
    else:
        # Reducing or flipping. Shares being closed = min(|held|, |fill|).
        closing = min(abs(held_shares), abs(fill_qty))
        # Per-share realised P&L is measured from the existing position's side:
        # long stock realises (exit − cost); short stock realises (cost − exit).
        direction = 1.0 if held_shares > 0 else -1.0
        realized_delta = (fill_price - avg_cost) * direction * closing
        realized_pnl += realized_delta
        if abs(fill_qty) > abs(held_shares):
            # Flipped: residual opens fresh at the fill price.
            avg_cost = fill_price
        elif new_held == 0:
            avg_cost = 0.0
        # else: partial close, avg_cost unchanged.

    return FillResult(new_held, avg_cost, realized_pnl, realized_delta)


def option_mark(
    *,
    spot: float,
    strike: float,
    t: float,
    r: float,
    sigma: float,
    kind: str,
    option_side: str,
    contracts: int,
    premium: float,
    multiplier: int = 100,
) -> tuple[float, float]:
    """Return ``(option_value, option_open_pnl)`` for the option leg.

    ``option_value`` is the current Black-Scholes value of the whole leg
    (per-share price × multiplier × contracts). ``option_open_pnl`` is the
    open profit on it: ``premium − value`` short, ``value − premium`` long.
    """
    per_share = black_scholes.price(spot, strike, t, r, sigma, kind)
    value = per_share * multiplier * contracts
    if option_side == "short":
        return value, premium - value
    if option_side == "long":
        return value, value - premium
    raise ValueError(f"option_side must be 'long' or 'short', got {option_side!r}")


def hedge_unrealized(held_shares: int, avg_cost: float, spot: float) -> float:
    """Mark-to-market P&L on the shares still held (signed position)."""
    return (spot - avg_cost) * held_shares


def net_open_pnl(
    *,
    option_open_pnl: float,
    realized_hedge_pnl: float,
    hedge_unrealized_pnl: float,
) -> float:
    """The headline number: option leg + realised stock + unrealised stock."""
    return option_open_pnl + realized_hedge_pnl + hedge_unrealized_pnl


@dataclass(frozen=True, slots=True)
class Settlement:
    """Terminal state of a hedge at expiry (or manual close)."""

    intrinsic_per_share: float
    option_settlement_pnl: float  # option leg's final P&L
    unwind_fill_qty: int  # shares traded to flatten the hedge (signed)
    realized_hedge_pnl: float  # hedge P&L after the unwind
    total_realized_pnl: float  # option + hedge, the final booked P&L
    in_the_money: bool


def settle(
    *,
    kind: str,
    option_side: str,
    strike: float,
    contracts: int,
    premium: float,
    held_shares: int,
    avg_cost: float,
    realized_hedge_pnl: float,
    spot: float,
    multiplier: int = 100,
) -> Settlement:
    """Cash-settle the option at intrinsic and unwind the stock hedge at spot.

    Cash-settling at intrinsic then selling the hedge shares at spot is P&L-
    identical to physical assignment (deliver shares at the strike), and far
    simpler to reason about. Works for both ITM (assignment/exercise) and OTM
    (expire worthless) — intrinsic is just zero in the OTM case.
    """
    if kind == "call":
        intrinsic = max(0.0, spot - strike)
        itm = spot > strike
    elif kind == "put":
        intrinsic = max(0.0, strike - spot)
        itm = spot < strike
    else:
        raise ValueError(f"kind must be 'call' or 'put', got {kind!r}")

    intrinsic_value = intrinsic * multiplier * contracts
    if option_side == "short":
        option_pnl = premium - intrinsic_value  # kept premium, pay intrinsic.
    elif option_side == "long":
        option_pnl = intrinsic_value - premium  # paid premium, collect intrinsic.
    else:
        raise ValueError(f"option_side must be 'long' or 'short', got {option_side!r}")

    # Flatten the stock hedge at spot.
    unwind_qty = -held_shares
    fill = apply_fill(
        held_shares=held_shares,
        avg_cost=avg_cost,
        realized_pnl=realized_hedge_pnl,
        fill_qty=unwind_qty,
        fill_price=spot,
    )

    return Settlement(
        intrinsic_per_share=intrinsic,
        option_settlement_pnl=option_pnl,
        unwind_fill_qty=unwind_qty,
        realized_hedge_pnl=fill.realized_pnl,
        total_realized_pnl=option_pnl + fill.realized_pnl,
        in_the_money=itm,
    )
