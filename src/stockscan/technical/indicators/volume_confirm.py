"""Volume confirmation — symmetric climax/absorption multiplier (spec §5).

Answers "did the reversal happen on a climax — a heavy-volume bar where price
rejected the extreme it was driving toward?" A bottom is a high-volume down bar
closing off its low (sellers spent, buyers absorbing); a top is a high-volume up
bar closing off its high (buyers spent). One formula, both directions — only the
bar's direction flips.

`kind="confirmation"`: returns a conviction multiplier in `[VOL_FLOOR, 1.0]`. It
can only ever *scale* the signed directional composite D — never flip its sign,
because it carries no sign of its own. Abstains (→ multiplier 1.0, no
attenuation) on insufficient history.

Pure math is in `_volume_values` (bars-only, no DB).
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

import pandas as pd
from pydantic import Field

from stockscan.technical.indicators.base import (
    TechnicalIndicator,
    TechnicalIndicatorParams,
)

if TYPE_CHECKING:
    from stockscan.strategies.base import Strategy


def _clip(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _volume_values(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    volume: pd.Series,
    *,
    rvol_window: int,
    spike_mult: float,
    vol_floor: float,
) -> dict[str, float] | None:
    if len(close) < rvol_window + 5:
        return None
    h, lo, cl = float(high.iloc[-1]), float(low.iloc[-1]), float(close.iloc[-1])
    prev = float(close.iloc[-2])
    rng = h - lo
    clv = 0.0 if rng == 0 else ((cl - lo) - (h - cl)) / rng  # close-location value, [-1,+1]

    med = volume.iloc[-rvol_window:].median()
    if pd.isna(med) or float(med) <= 0:
        return None
    rvol_med = float(volume.iloc[-1]) / float(med)
    spike_s = _clip((rvol_med - spike_mult) / spike_mult)

    # Rejection = the bar closing AGAINST the direction it moved that day.
    if cl < prev:
        reject = clv  # down day closing off its low → bottom absorption
    elif cl > prev:
        reject = -clv  # up day closing off its high → top absorption
    else:
        reject = 0.0
    absorb_s = _clip((reject + 1.0) / 2.0)

    multiplier = vol_floor + (1.0 - vol_floor) * _clip(spike_s * absorb_s)
    return {
        "rvol_med": round(rvol_med, 4),
        "clv": round(clv, 4),
        "reject": round(reject, 4),
        "multiplier": round(multiplier, 4),
    }


class VolumeConfirmParams(TechnicalIndicatorParams):
    rvol_window: int = Field(50, ge=10, le=200, description="Median-volume baseline window.")
    spike_mult: float = Field(1.5, gt=0, description="Spike vs median that begins to count.")
    vol_floor: float = Field(0.75, ge=0, le=1, description="Lowest conviction multiplier.")


class TechnicalVolumeConfirm(TechnicalIndicator):
    name = "volume_confirm"
    description = (
        "Symmetric climax/absorption multiplier: a volume spike where the bar "
        "rejected its own direction (down→close-off-low, up→close-off-high). "
        "Scales the reversal conviction; never flips its sign."
    )
    params_model = VolumeConfirmParams
    kind = "confirmation"

    def values(self, bars: pd.DataFrame, as_of: date) -> dict[str, float] | None:
        for col in ("high", "low", "close", "volume"):
            if col not in bars.columns:
                return None
        p: VolumeConfirmParams = self.params  # type: ignore[assignment]
        return _volume_values(
            bars["high"],
            bars["low"],
            bars["close"],
            bars["volume"],
            rvol_window=p.rvol_window,
            spike_mult=p.spike_mult,
            vol_floor=p.vol_floor,
        )

    def score(self, values: dict[str, float], strategy: type[Strategy] | None) -> float:
        # Confirmation kind: return the [VOL_FLOOR, 1] multiplier (not a signed vote).
        return values["multiplier"]
