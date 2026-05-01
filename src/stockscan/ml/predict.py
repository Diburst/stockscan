"""Score one signal under the trained meta-labeling model.

Usage:

    from stockscan.ml import score_signal

    proba = score_signal(
        strategy_name="donchian_trend",
        bars=bars_df,
        as_of=as_of,
        signal_metadata=raw_signal.metadata,
        signal_score=float(raw_signal.score),
        regime=regime_row,
    )
    if proba is not None:
        signal.metadata["meta_label_proba"] = round(proba, 4)

Soft-fails on every error path: missing model file, schema skew, NaN
features. The runner integration treats ``None`` as "no advisory
score" and proceeds without it. We never want a meta-label issue to
break a scan.
"""

from __future__ import annotations

import functools
import logging
from typing import TYPE_CHECKING, Any

import pandas as pd

from stockscan.ml.features import FEATURE_COLUMNS, build_features
from stockscan.ml.store import ModelArtifact, load_model

if TYPE_CHECKING:
    from datetime import date

    from stockscan.regime.store import MarketRegime

log = logging.getLogger(__name__)


# Lazy per-strategy model cache. The runner calls score_signal() once
# per emitted signal — without a cache we'd hit disk + unpickle on
# every call, which is wasteful for a 500-symbol scan run. Invalidated
# manually via :func:`clear_cache` (CLI re-train flushes it).
@functools.lru_cache(maxsize=8)
def _cached_load(strategy_name: str) -> ModelArtifact | None:
    """LRU-cached loader. ``None`` is cached too — that's intentional;
    a strategy that has never been trained shouldn't probe the disk on
    every signal during a scan run."""
    return load_model(strategy_name)


def clear_cache() -> None:
    """Drop the model cache. Call after re-training so the next
    score_signal picks up the new artifact without an app restart."""
    _cached_load.cache_clear()


def score_signal(
    *,
    strategy_name: str,
    bars: pd.DataFrame,
    as_of: date,
    signal_metadata: dict[str, Any] | None = None,
    signal_score: float | None = None,
    regime: MarketRegime | None = None,
) -> float | None:
    """Return ``P(signal hits profit-take)`` ∈ [0, 1], or ``None``.

    Returns ``None`` when:
      * no trained model exists for ``strategy_name`` yet,
      * the loaded artifact's feature columns don't match the live
        :data:`FEATURE_COLUMNS` (schema skew — re-train),
      * feature build fails (insufficient bars history),
      * the model prediction itself raises (xgboost version skew).

    The probability is the model's confidence that this signal will
    hit its profit-take barrier within the holding window — NOT a
    return forecast. Treat thresholds with care: 0.55 is the natural
    starting point but the right cutoff depends on base rate and
    desired precision/recall trade-off.
    """
    artifact = _cached_load(strategy_name)
    if artifact is None or artifact.model is None:
        return None

    # Schema check. ``feature_columns`` was pickled at train time;
    # if FEATURE_COLUMNS has shifted underneath it (we appended a
    # new feature, reordered, etc.), bail to None rather than feed
    # mismatched columns.
    if tuple(artifact.feature_columns) != tuple(FEATURE_COLUMNS):
        log.warning(
            "score_signal: feature schema skew for %s — model expects %d cols, "
            "live FEATURE_COLUMNS has %d. Re-train.",
            strategy_name,
            len(artifact.feature_columns),
            len(FEATURE_COLUMNS),
        )
        return None

    try:
        features = build_features(
            bars=bars,
            as_of=as_of,
            signal_metadata=signal_metadata,
            regime=regime,
            signal_score=signal_score,
        )
    except Exception as exc:
        log.warning("score_signal: feature build failed: %s", exc)
        return None

    # Single-row DataFrame in the canonical column order. (Lower-case
    # ``x`` rather than the sklearn-conventional ``X`` to satisfy the
    # project's pep8-naming rule set; semantically identical.)
    x = pd.DataFrame([features], columns=list(FEATURE_COLUMNS))
    try:
        proba = artifact.model.predict_proba(x)[:, 1]
    except Exception as exc:
        log.warning("score_signal: model.predict_proba raised: %s", exc)
        return None
    if proba is None or len(proba) == 0:
        return None
    val = float(proba[0])
    # Clip to [0, 1] just in case (some classifiers can return marginally
    # outside this range due to numerical issues).
    return max(0.0, min(1.0, val))
