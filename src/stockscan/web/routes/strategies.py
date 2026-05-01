"""Strategies page — read-only metadata view + meta-label model status."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request

from stockscan.ml import list_models, load_model
from stockscan.strategies import STRATEGY_REGISTRY, discover_strategies
from stockscan.web.deps import render

router = APIRouter(prefix="/strategies")
log = logging.getLogger(__name__)


def _models_by_strategy() -> dict[str, Any]:
    """Build a name→ModelArtifact lookup once per request.

    list_models() walks the ./models directory. Soft-fails to an
    empty dict on any error so the strategies page never breaks
    because of a corrupted artifact.
    """
    try:
        models = list_models()
    except Exception as exc:  # never break the page on ML issues
        log.warning("strategies: list_models() failed: %s", exc)
        return {}
    return {a.strategy_name: a for a in models}


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
    try:
        model_artifact = load_model(name)
    except Exception as exc:
        log.warning("strategies/%s: load_model() failed: %s", name, exc)
        model_artifact = None

    return render(
        request,
        "strategies/detail.html",
        strategy=cls,
        schema=cls.params_json_schema(),
        model=model_artifact,
    )
