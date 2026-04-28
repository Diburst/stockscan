"""Strategies page — read-only metadata view (parameter editor in v1.5)."""

from __future__ import annotations

from fastapi import APIRouter, Request

from stockscan.strategies import STRATEGY_REGISTRY, discover_strategies
from stockscan.web.deps import render

router = APIRouter(prefix="/strategies")


@router.get("")
async def strategies_list(request: Request):
    discover_strategies()
    return render(request, "strategies/list.html", strategies=STRATEGY_REGISTRY.all())


@router.get("/{name}")
async def strategy_detail(name: str, request: Request):
    discover_strategies()
    try:
        cls = STRATEGY_REGISTRY.get(name)
    except KeyError:
        return render(request, "strategies/detail.html", strategy=None)
    return render(
        request,
        "strategies/detail.html",
        strategy=cls,
        schema=cls.params_json_schema(),
    )
