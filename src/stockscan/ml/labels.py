"""Triple-barrier labeling for historical signals.

Lopez de Prado's triple-barrier method (Advances in Financial Machine
Learning, 2018, ch. 3) — for each historical signal we look forward
and assign a binary label by checking which of three "barriers" is
hit first:

  1. **Profit-take barrier** (upper). Default: entry + 2 x ATR(20).
     If price closes at-or-above this within the holding window →
     label = 1 (winner).
  2. **Stop-loss barrier** (lower). Defaults to the strategy's own
     ``suggested_stop`` from the signal — that's what would actually
     have been used live, so labels match real PnL semantics.
     If hit first → label = 0 (loser).
  3. **Time barrier**. Default: 20 trading days. If neither price
     barrier is hit in time, fall back to the SIGN of the
     final-day return: positive → 1, negative-or-zero → 0.

The function is pure — bars in, label out — so it's trivial to run
across every persisted historical signal in train.py and to unit-test
in isolation. No DB calls live here.

Notes:
  * "Hit" means the bar's high (for the profit-take) or low (for the
    stop) crosses the barrier intra-day. Even if the close is on the
    right side of the barrier, an intraday touch counts — that's the
    realistic exit semantics.
  * If the profit-take and stop are touched on the same bar (a wide-
    range day), we conservatively label as a stop-out. Real fills
    don't tell you which came first within a single daily bar.
  * Signals without enough forward bars (e.g., very recent ones) get
    a ``None`` label and are dropped from the training set rather
    than guessed at.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import date
    from decimal import Decimal

    import pandas as pd


class TripleBarrierLabel(IntEnum):
    """Outcome label for one signal under the triple-barrier rule.

    Cast to int implicitly when fed to XGBoost; we keep the enum
    around for log-readability in train.py.
    """

    LOSER = 0
    WINNER = 1


@dataclass(frozen=True, slots=True)
class _BarrierOutcome:
    """Internal — what triggered the label, used only for diagnostics."""

    label: TripleBarrierLabel
    trigger: str  # "profit_take" | "stop_loss" | "time_positive" | "time_nonpositive"
    days_held: int


def triple_barrier_label(
    bars_after: pd.DataFrame,
    *,
    entry: Decimal | float,
    stop: Decimal | float,
    profit_take: Decimal | float | None = None,
    profit_take_atr_mult: float = 2.0,
    atr_at_entry: Decimal | float | None = None,
    max_days: int = 20,
) -> TripleBarrierLabel | None:
    """Apply the triple-barrier rule to one signal's forward bars.

    Parameters
    ----------
    bars_after:
        DataFrame of bars STRICTLY AFTER the signal's entry day (i.e.,
        the first row should be the bar where the trade would have
        been entered — typically next-open). Indexed chronologically.
        Must contain ``high``, ``low``, ``close`` columns.
    entry:
        The trade's notional entry price (``RawSignal.suggested_entry``).
    stop:
        The trade's stop price (``RawSignal.suggested_stop``).
    profit_take:
        Explicit profit-take level. If ``None``, derived as
        ``entry + profit_take_atr_mult x atr_at_entry`` — supply
        ``atr_at_entry`` for that path.
    profit_take_atr_mult:
        Used only when computing the implicit profit-take from ATR.
    atr_at_entry:
        ATR at the signal's as_of day. Used to derive the implicit
        profit-take.
    max_days:
        Maximum holding period in trading days. Bars beyond this are
        ignored.

    Returns
    -------
    TripleBarrierLabel | None
        ``None`` if there aren't enough forward bars to apply the
        rule (drop from training); otherwise WINNER (1) or LOSER (0).
    """
    if bars_after is None or len(bars_after) == 0:
        return None

    pt = _resolve_profit_take(
        entry=float(entry),
        profit_take=profit_take,
        profit_take_atr_mult=profit_take_atr_mult,
        atr_at_entry=atr_at_entry,
    )
    if pt is None:
        return None

    sl = float(stop)
    en = float(entry)
    if not (sl < en < pt):
        # Nonsensical barrier ordering (e.g., stop above entry, or
        # profit-take below entry). Skip — likely a data glitch.
        return None

    window = bars_after.iloc[:max_days]
    if len(window) == 0:
        return None

    outcome = _walk_barriers(window, entry=en, stop=sl, profit_take=pt)
    return outcome.label


def _resolve_profit_take(
    *,
    entry: float,
    profit_take: Decimal | float | None,
    profit_take_atr_mult: float,
    atr_at_entry: Decimal | float | None,
) -> float | None:
    if profit_take is not None:
        return float(profit_take)
    if atr_at_entry is None:
        return None
    atr_v = float(atr_at_entry)
    if atr_v <= 0:
        return None
    return entry + profit_take_atr_mult * atr_v


def _walk_barriers(
    bars: pd.DataFrame,
    *,
    entry: float,
    stop: float,
    profit_take: float,
) -> _BarrierOutcome:
    """Walk forward bar-by-bar, return the first-hit outcome."""
    for i, (_, row) in enumerate(bars.iterrows()):
        hi = float(row["high"])
        lo = float(row["low"])
        # If both touched on the same day → conservatively a stop.
        if lo <= stop and hi >= profit_take:
            return _BarrierOutcome(
                label=TripleBarrierLabel.LOSER,
                trigger="stop_loss",
                days_held=i + 1,
            )
        if lo <= stop:
            return _BarrierOutcome(
                label=TripleBarrierLabel.LOSER,
                trigger="stop_loss",
                days_held=i + 1,
            )
        if hi >= profit_take:
            return _BarrierOutcome(
                label=TripleBarrierLabel.WINNER,
                trigger="profit_take",
                days_held=i + 1,
            )

    # Time barrier: neither price barrier hit. Use the sign of the
    # final-day return relative to entry.
    final_close = float(bars["close"].iloc[-1])
    label = (
        TripleBarrierLabel.WINNER if final_close > entry else TripleBarrierLabel.LOSER
    )
    trigger = "time_positive" if final_close > entry else "time_nonpositive"
    return _BarrierOutcome(label=label, trigger=trigger, days_held=len(bars))


# ---------------------------------------------------------------------------
# Helper: pull forward bars for a historical signal's training window.
# ---------------------------------------------------------------------------


def select_forward_bars(
    bars: pd.DataFrame,
    as_of: date,
    *,
    max_days: int = 20,
) -> pd.DataFrame:
    """Slice ``bars`` to the rows STRICTLY AFTER ``as_of``, capped at max_days.

    Used by train.py to build a (features, label) row per historical
    signal. Strict-greater-than because the signal fires at
    ``as_of`` close and the trade enters at the next open.
    """
    idx_dates = bars.index.date if hasattr(bars.index, "date") else None
    if idx_dates is None:
        return bars.head(max_days)
    forward_mask = idx_dates > as_of
    return bars[forward_mask].head(max_days)
