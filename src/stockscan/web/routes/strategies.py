"""Strategies page — read-only metadata view + meta-label model status."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from stockscan.ml import list_models, load_model
from stockscan.strategies import STRATEGY_REGISTRY, discover_strategies
from stockscan.web.deps import render, safe

router = APIRouter(prefix="/strategies")


def _models_by_strategy() -> dict[str, Any]:
    """Build a name→ModelArtifact lookup once per request.

    Soft-fails to an empty dict on any error so the strategies page never
    breaks because of a corrupted artifact.
    """
    models = safe(list_models, default=[], label="strategies.list_models")
    return {a.strategy_name: a for a in models or []}


@router.get("")
def strategies_list(request: Request):
    """Read-only list of every registered strategy, each annotated with its
    meta-label model status (soft-fails to no models on artifact errors)."""
    discover_strategies()
    return render(
        request,
        "strategies/list.html",
        strategies=STRATEGY_REGISTRY.all(),
        models_by_strategy=_models_by_strategy(),
    )


@router.get("/{name}")
def strategy_detail(name: str, request: Request):
    """Single strategy view: metadata, the params JSON schema or — for
    strategies that keep their knobs as ClassVar constants — a tuning-knobs
    table built from the class attributes, plus its meta-label model
    artifact. Unknown names render the empty-state page."""
    discover_strategies()
    try:
        cls = STRATEGY_REGISTRY.get(name)
    except KeyError:
        return render(request, "strategies/detail.html", strategy=None, model=None)

    # Load this strategy's model artifact directly (single-file lookup;
    # cheaper than walking the whole models dir).
    model_artifact = safe(lambda: load_model(name), label=f"load_model[{name}]")

    # Strategies that keep their knobs as ClassVar constants in the file
    # (no params_model) get a "Tuning knobs" table built from the class's
    # own primitive attributes; strategies with a params_model fall through
    # to the JSON-schema panel.
    knobs: list[tuple[str, object]] | None = None
    if cls.params_model is None:
        skip = {"name", "version", "display_name", "description", "manual",
                "tags", "regime_affinity", "default_affinity",
                "applicable_regimes", "params_model"}
        knobs = [
            (k, v) for k, v in vars(cls).items()
            if not k.startswith("_")
            and k not in skip
            and isinstance(v, (int, float, str, bool))
        ]

    return render(
        request,
        "strategies/detail.html",
        strategy=cls,
        schema=cls.params_json_schema(),
        knobs=knobs,
        model=model_artifact,
    )
