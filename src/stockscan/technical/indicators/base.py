"""TechnicalIndicator ABC + auto-registry.

Mirrors the Strategy plugin pattern: subclassing this class registers the
indicator. Each indicator has two responsibilities:

  1. `values(bars, as_of)` — compute raw indicator values from bars
     (e.g., {"value": 28.4} for RSI). Returns None if there's insufficient
     history; the composite scorer skips indicators that abstain.

  2. `score(values, strategy)` — map raw values to a confirmation score in
     [-1, +1] given the firing strategy. The branching is by `strategy.tags`,
     not by strategy name, so adding a new strategy of an existing kind
     ("mean_reversion", etc.) doesn't require touching indicator code.
     If `strategy is None`, indicators return a "neutral" direction-agnostic
     bullish-bias score — used by the watchlist (no firing strategy).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from typing import ClassVar

import pandas as pd
from pydantic import BaseModel

from stockscan.strategies.base import Strategy


class TechnicalIndicatorParams(BaseModel):
    """Subclass per indicator. Pydantic gives validation + JSON schema."""


class _Registry:
    def __init__(self) -> None:
        self._by_name: dict[str, type[TechnicalIndicator]] = {}

    def register(self, cls: type[TechnicalIndicator]) -> None:
        if cls.name in self._by_name and self._by_name[cls.name] is not cls:
            existing = self._by_name[cls.name]
            raise ValueError(
                f"TechnicalIndicator name collision: '{cls.name}' is already "
                f"registered to {existing.__module__}.{existing.__qualname__}"
            )
        self._by_name[cls.name] = cls

    def get(self, name: str) -> type[TechnicalIndicator]:
        if name not in self._by_name:
            raise KeyError(f"Unknown indicator '{name}'. Registered: {sorted(self._by_name)}")
        return self._by_name[name]

    def all(self) -> list[type[TechnicalIndicator]]:
        return list(self._by_name.values())

    def names(self) -> list[str]:
        return sorted(self._by_name)

    def __len__(self) -> int:
        return len(self._by_name)

    def __contains__(self, name: str) -> bool:
        return name in self._by_name


TECH_REGISTRY = _Registry()


def _clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


class TechnicalIndicator(ABC):
    """Contract for one technical indicator that contributes to the score."""

    name: ClassVar[str]
    description: ClassVar[str] = ""
    params_model: ClassVar[type[TechnicalIndicatorParams]]

    __abstract__: ClassVar[bool] = False

    def __init__(self, params: TechnicalIndicatorParams | None = None) -> None:
        self.params = params or self.params_model()

    # --- contract methods ---
    @abstractmethod
    def values(self, bars: pd.DataFrame, as_of: date) -> dict[str, float] | None:
        """Compute raw values (e.g., {"value": 42.0}). Return None if there's
        insufficient history — the composite scorer will skip this indicator
        rather than score on partial data."""

    @abstractmethod
    def score(
        self,
        values: dict[str, float],
        strategy: type[Strategy] | None,
    ) -> float:
        """Map raw values to a confirmation score in [-1, +1].

        If `strategy is None`, return a direction-agnostic bullish-bias score
        (used by the watchlist where no strategy is firing).
        """

    # --- helpers ---
    @staticmethod
    def clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
        return _clamp(x, lo, hi)

    # --- auto-registration ---
    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if getattr(cls, "__abstract__", False):
            return
        for attr in ("name", "params_model"):
            if not hasattr(cls, attr):
                raise TypeError(
                    f"TechnicalIndicator subclass {cls.__name__} is missing required "
                    f"class attribute '{attr}'."
                )
        TECH_REGISTRY.register(cls)
