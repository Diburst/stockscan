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
async def strategies_list(request: Request):
    discover_strategies()
    return render(
        request,
        "strategies/list.html",
        strategies=STRATEGY_REGISTRY.all(),
        models_by_strategy=_models_by_strategy(),
    )


@router.get("/{name}")
async def strategy_detail(name: str, request: Request):
    discover_strategies()
    try:
        cls = STRATEGY_REGISTRY.get(name)
    except KeyError:
        return render(request, "strategies/detail.html", strategy=None, model=None)

    # Load this strategy's model artifact directly (single-file lookup;
    # cheaper than walking the whole models dir).
    model_artifact = safe(lambda: load_model(name), label=f"load_model[{name}]")

    return render(
        request,
        "strategies/detail.html",
        strategy=cls,
        schema=cls.params_json_schema(),
        model=model_artifact,
    )
