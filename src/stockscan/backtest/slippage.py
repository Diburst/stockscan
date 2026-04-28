"""Slippage models for the backtester.

Slippage is applied at fill time, in the direction that hurts (worse fill
price for the trader). Backtests should default to a conservative model
(5 bps for liquid US equities); switch to NoSlippage only for sensitivity
testing.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal


class SlippageModel(ABC):
    """Returns the actual fill price given a side and reference price."""

    @abstractmethod
    def adjust(self, side: str, reference_price: Decimal, qty: int = 0) -> Decimal:
        """side ∈ {'buy', 'sell'}; returns the worse-than-reference fill price."""


@dataclass(frozen=True, slots=True)
class NoSlippage(SlippageModel):
    """For sensitivity testing only. Real markets always have slippage."""

    def adjust(self, side: str, reference_price: Decimal, qty: int = 0) -> Decimal:
        return reference_price


@dataclass(frozen=True, slots=True)
class FixedBpsSlippage(SlippageModel):
    """Fixed bps off the reference price in the direction that hurts."""

    bps: Decimal = Decimal("5")  # 5 bps = 0.05%

    def adjust(self, side: str, reference_price: Decimal, qty: int = 0) -> Decimal:
        adj = reference_price * (self.bps / Decimal("10000"))
        return reference_price + adj if side == "buy" else reference_price - adj


@dataclass(frozen=True, slots=True)
class VolumeBasedSlippage(SlippageModel):
    """Slippage scales with order size as fraction of daily volume.

    Coarse model: extra bps proportional to (qty / typical_daily_volume).
    Useful for testing how strategies degrade as size grows.
    """

    base_bps: Decimal = Decimal("5")
    impact_bps_per_pct_volume: Decimal = Decimal("2")  # +2 bps per 1% of volume
    typical_daily_volume: int = 1_000_000

    def adjust(self, side: str, reference_price: Decimal, qty: int = 0) -> Decimal:
        pct_volume = Decimal(qty) / Decimal(max(1, self.typical_daily_volume)) * Decimal("100")
        total_bps = self.base_bps + self.impact_bps_per_pct_volume * pct_volume
        adj = reference_price * (total_bps / Decimal("10000"))
        return reference_price + adj if side == "buy" else reference_price - adj
