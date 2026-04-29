"""Strategy ABC + registry — DESIGN §4.11.

Every strategy is a subclass of `Strategy`. Subclassing triggers automatic
registration via `__init_subclass__`. The scanner, backtester, and base-rate
analyzer iterate `STRATEGY_REGISTRY` and never import strategies by name.
"""

from __future__ import annotations

import hashlib
import inspect
import json
from abc import ABC, abstractmethod
from datetime import date
from typing import ClassVar

import pandas as pd
from pydantic import BaseModel

from stockscan.strategies._signals import (
    ExitDecision,
    PositionSnapshot,
    RawSignal,
)


class StrategyParams(BaseModel):
    """Subclass per strategy. Pydantic provides validation + JSON schema export."""


class _Registry:
    """In-process registry. Populated by `__init_subclass__` on Strategy."""

    def __init__(self) -> None:
        self._by_name: dict[str, type[Strategy]] = {}

    def register(self, cls: type[Strategy]) -> None:
        if cls.name in self._by_name and self._by_name[cls.name] is not cls:
            existing = self._by_name[cls.name]
            raise ValueError(
                f"Strategy name collision: '{cls.name}' is already registered to "
                f"{existing.__module__}.{existing.__qualname__}; cannot register "
                f"{cls.__module__}.{cls.__qualname__}"
            )
        self._by_name[cls.name] = cls

    def get(self, name: str) -> type[Strategy]:
        if name not in self._by_name:
            raise KeyError(
                f"Unknown strategy '{name}'. Registered: {sorted(self._by_name)}"
            )
        return self._by_name[name]

    def all(self) -> list[type[Strategy]]:
        return list(self._by_name.values())

    def names(self) -> list[str]:
        return sorted(self._by_name)

    def clear(self) -> None:
        """Test-only: drop all registrations."""
        self._by_name.clear()

    def __contains__(self, name: str) -> bool:
        return name in self._by_name

    def __len__(self) -> int:
        return len(self._by_name)


STRATEGY_REGISTRY = _Registry()


class Strategy(ABC):
    """Strategy contract.

    Subclasses must declare the class attributes (`name`, `version`,
    `display_name`, `description`, `params_model`) and implement
    `required_history`, `signals`, and `exit_rules`.

    Subclassing this triggers automatic registration.
    """

    # ----- declarative metadata (override in subclasses) -----
    name: ClassVar[str]
    version: ClassVar[str]
    display_name: ClassVar[str]
    description: ClassVar[str] = ""           # one-paragraph teaser (UI cards)
    manual: ClassVar[str] = ""                # long-form, beginner-friendly walkthrough
    tags: ClassVar[tuple[str, ...]] = ()
    params_model: ClassVar[type[StrategyParams]]
    default_risk_pct: ClassVar[float] = 0.01

    # Regimes in which this strategy is permitted to generate signals.
    # Empty frozenset = "runs in all regimes" (no restriction).
    # Labels: 'trending_up', 'trending_down', 'choppy', 'transitioning'
    applicable_regimes: ClassVar[frozenset[str]] = frozenset()

    # Subclasses set this to True if they should NOT be auto-registered
    # (e.g., abstract intermediate base classes).
    __abstract__: ClassVar[bool] = False

    def __init__(self, params: StrategyParams) -> None:
        if not isinstance(params, self.params_model):
            raise TypeError(
                f"{type(self).__name__} expected params of type "
                f"{self.params_model.__name__}, got {type(params).__name__}"
            )
        self.params = params

    # ----- contract methods -----
    @abstractmethod
    def required_history(self) -> int:
        """Bars needed before signals() can produce output."""

    @abstractmethod
    def signals(self, bars: pd.DataFrame, as_of: date) -> list[RawSignal]:
        """Pure function. MUST NOT use bars after `as_of`."""

    @abstractmethod
    def exit_rules(
        self,
        position: PositionSnapshot,
        bars: pd.DataFrame,
        as_of: date,
    ) -> ExitDecision | None:
        """Return an exit decision, or None to hold."""

    # ----- helpers used by the framework -----
    @classmethod
    def code_fingerprint(cls) -> str:
        """SHA-256 of the strategy's source file. Used to detect code drift."""
        try:
            src_file = inspect.getsourcefile(cls)
            if src_file is None:
                return "unknown"
            with open(src_file, "rb") as f:
                return hashlib.sha256(f.read()).hexdigest()
        except (TypeError, OSError):
            return "unknown"

    @classmethod
    def params_json_schema(cls) -> dict[str, object]:
        """JSON Schema for the strategy's params (used by UI form rendering)."""
        return cls.params_model.model_json_schema()

    @classmethod
    def hash_params(cls, params: StrategyParams) -> str:
        """Stable SHA-256 of canonical-JSON params. Used for params_hash in DB."""
        canonical = json.dumps(
            params.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    # ----- auto-registration -----
    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if getattr(cls, "__abstract__", False):
            return
        # Verify required class attributes exist before registering.
        for attr in ("name", "version", "display_name", "params_model"):
            if not hasattr(cls, attr):
                raise TypeError(
                    f"Strategy subclass {cls.__name__} is missing required class "
                    f"attribute '{attr}'."
                )
        STRATEGY_REGISTRY.register(cls)
