"""Strategy-inspection tools (read-only, no DB)."""

from __future__ import annotations

from typing import Any

from stockscan.mcp.serialize import jsonable
from stockscan.strategies import STRATEGY_REGISTRY, discover_strategies


def _summary(cls: Any) -> dict[str, Any]:
    return {
        "name": cls.name,
        "version": cls.version,
        "display_name": cls.display_name,
        "description": cls.description,
        "tags": list(cls.tags),
        "regime_affinity": jsonable(getattr(cls, "regime_affinity", {}) or {}),
        "default_risk_pct": jsonable(getattr(cls, "default_risk_pct", None)),
    }


def list_strategies() -> dict[str, Any]:
    """List every registered trading strategy with its name, version, and tags.

    Returns:
        {"strategies": [{name, version, display_name, description, tags,
        regime_affinity, default_risk_pct}, ...]}.
    """
    discover_strategies()
    return {"strategies": [_summary(c) for c in STRATEGY_REGISTRY.all()]}


def get_strategy(name: str) -> dict[str, Any]:
    """Get one strategy's full detail, including its long-form trader manual.

    Args:
        name: The strategy's short name (e.g. "rsi2_meanrev").

    Returns:
        The strategy summary plus its ``manual`` text, or {"error":
        "unknown_strategy", "known": [...]} if the name isn't registered.
    """
    discover_strategies()
    if name not in STRATEGY_REGISTRY.names():
        return {
            "error": "unknown_strategy",
            "name": name,
            "known": STRATEGY_REGISTRY.names(),
        }
    cls = STRATEGY_REGISTRY.get(name)
    out = _summary(cls)
    out["manual"] = getattr(cls, "manual", None)
    return out
