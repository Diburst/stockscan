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
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from datetime import date

    import pandas as pd

    from stockscan.strategies._signals import (
        ExitDecision,
        PositionSnapshot,
        RawSignal,
    )

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
            raise KeyError(f"Unknown strategy '{name}'. Registered: {sorted(self._by_name)}")
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

    Subclasses declare the class attributes (`name`, `version`,
    `display_name`, `description`), keep every tunable knob as a ClassVar
    constant on the class (edit the file and bump `version` to change one),
    and implement `required_history`, `signals`, and `exit_rules`.

    Subclassing this triggers automatic registration.
    """

    # ----- declarative metadata (override in subclasses) -----
    name: ClassVar[str]
    version: ClassVar[str]
    display_name: ClassVar[str]
    description: ClassVar[str] = ""  # one-paragraph teaser (UI cards)
    manual: ClassVar[str] = ""  # long-form, beginner-friendly walkthrough
    tags: ClassVar[tuple[str, ...]] = ()

    # ----- Sizing -----
    # Stop-based strategies risk ``default_risk_pct`` of equity per trade;
    # share count follows from the stop distance. Strategies that emit no
    # stop (mean reversion, where stops hurt — Kaminski & Lo 2014) set
    # ``position_pct`` instead and get a fixed fraction of equity per slot.
    default_risk_pct: ClassVar[float] = 0.01
    position_pct: ClassVar[float | None] = None
    # Cap on this strategy's own open positions (the portfolio-wide cap in
    # config still applies). None = only the portfolio cap.
    max_open_positions: ClassVar[int | None] = None
    # Whether the regime layer's realized-vol scalar shrinks this strategy's
    # size in high-vol markets. True for momentum (that is where momentum
    # crashes); False for mean reversion (reversal pays best in high vol).
    sizes_down_in_high_vol: ClassVar[bool] = True
    # Non-bar data the strategy reads from the DB (provider feature names,
    # see stockscan.data.providers.base.ALL_FEATURES). Bars are implicit.
    # Purely informational: the strategy page uses it to show "fundamentals
    # snapshot as of <date>" and to flag when the data plan no longer
    # refreshes that input. Empty = bars only.
    data_dependencies: ClassVar[tuple[str, ...]] = ()

    # Subclasses set this to True if they should NOT be auto-registered
    # (e.g., abstract intermediate base classes).
    __abstract__: ClassVar[bool] = False

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
        """Return an exit decision, or None to hold.

        Exits — including any stop — are the strategy's decision alone; the
        engine and the live runner apply no stop of their own.
        """

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

    _METADATA_ATTRS: ClassVar[frozenset[str]] = frozenset(
        {"name", "version", "display_name", "description", "manual", "tags",
         "data_dependencies"}
    )

    @classmethod
    def knobs(cls) -> dict[str, int | float | str | bool]:
        """Every tunable constant declared on this class, for the strategy
        page, the run record and the CLI. Sizing attributes count as knobs."""
        out: dict[str, int | float | str | bool] = {}
        for klass in reversed(cls.__mro__):
            for key, value in vars(klass).items():
                if key.startswith("_") or key in cls._METADATA_ATTRS:
                    continue
                if isinstance(value, (int, float, str, bool)) and not isinstance(value, type):
                    out[key] = value
        return out

    @classmethod
    def knobs_hash(cls) -> str:
        """Stable SHA-256 of the knob dict — the run record's identity for
        'which settings produced this'."""
        canonical = json.dumps(cls.knobs(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    # ----- auto-registration -----
    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if getattr(cls, "__abstract__", False):
            return
        for attr in ("name", "version", "display_name"):
            if not hasattr(cls, attr):
                raise TypeError(
                    f"Strategy subclass {cls.__name__} is missing required class "
                    f"attribute '{attr}'."
                )

        STRATEGY_REGISTRY.register(cls)
