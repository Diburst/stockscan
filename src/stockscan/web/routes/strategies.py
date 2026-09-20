"""Strategies page — read-only view of each strategy's knobs and sizing."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from stockscan.config import settings
from stockscan.strategies import STRATEGY_REGISTRY, discover_strategies
from stockscan.web.deps import get_session, render, safe

router = APIRouter(prefix="/strategies")

_LATEST_SECTOR_BAR_SQL = text(
    "SELECT MAX(bar_ts)::date FROM bars WHERE symbol LIKE '$EWSECTOR:%' AND interval = '1d'"
)


@router.get("")
def strategies_list(request: Request):
    """Read-only list of every registered strategy."""
    discover_strategies()
    return render(request, "strategies/list.html", strategies=STRATEGY_REGISTRY.all())


@router.get("/{name}")
def strategy_detail(name: str, request: Request, s: Session = Depends(get_session)):
    """Single strategy view: metadata, the sizing rule, the tuning-knobs
    table read off the class, and the freshness of any non-bar data it
    depends on. Unknown names render the empty-state page."""
    discover_strategies()
    try:
        cls = STRATEGY_REGISTRY.get(name)
    except KeyError:
        return render(request, "strategies/detail.html", strategy=None)

    # Freshness of non-bar inputs. Sector composites are built locally from
    # bars plus the fundamentals sector map, so they keep refreshing on any
    # plan; provider feature families only refresh when the plan allows.
    data_inputs: list[dict[str, object]] = []
    for feature in cls.data_dependencies:
        as_of = None
        refreshing = feature in settings.eodhd_feature_set
        if feature == "fundamentals":
            from stockscan.fundamentals.store import snapshot_as_of

            as_of = safe(snapshot_as_of, label="fundamentals.snapshot_as_of")
        elif feature == "sector_composites":
            row = safe(
                lambda: s.execute(_LATEST_SECTOR_BAR_SQL).first(),
                label="strategies.latest_sector_bar",
            )
            as_of = row[0] if row is not None else None
            refreshing = True
        data_inputs.append({"feature": feature, "as_of": as_of, "refreshing": refreshing})

    return render(
        request,
        "strategies/detail.html",
        strategy=cls,
        knobs=cls.knobs(),
        data_inputs=data_inputs,
    )
