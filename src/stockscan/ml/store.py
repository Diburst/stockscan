"""On-disk model artifact storage.

Layout under the project root::

    models/
      <strategy_name>/
        latest.pkl                 ← symlink-equivalent: convenience pointer
        <strategy_name>_<iso>.pkl  ← timestamped artifact

Each pickle is a :class:`ModelArtifact` — the trained estimator
plus the metadata you need to verify it at score time (feature
column order, training dataset stats, model version, fit timestamp).
``predict.py`` cross-checks ``feature_columns`` against the live
features on every prediction; a mismatch hard-fails rather than
silently misaligning a column.

Why pickle and not e.g. a model-server: scale doesn't justify it.
We re-train at most weekly per strategy; a 50KB pickle on local disk
is the right impedance match.

A slightly more elaborate version of this would store a hash of the
training data so we can detect "fit on stale snapshot" — defer until
we actually have enough data to drift, which we don't yet.
"""

from __future__ import annotations

import logging
import pickle
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

log = logging.getLogger(__name__)


# Default models directory, anchored to the repo root (parent of
# `src/`). Override via the STOCKSCAN_MODELS_DIR env var if you want
# to keep models outside the repo. Resolved lazily so tests can
# tweak the env without re-importing.
def _models_root() -> Path:
    """Return the root directory for model artifacts.

    Resolved lazily on each call so test setup can patch the env var
    after import. Creates the directory if it doesn't exist (cheap;
    anybody calling save_model wants this anyway).
    """
    import os

    override = os.environ.get("STOCKSCAN_MODELS_DIR")
    if override:
        return Path(override).expanduser().resolve()
    # Default: <repo_root>/models. We anchor to this file's location:
    # src/stockscan/ml/store.py → ../../../ → repo root.
    return (Path(__file__).resolve().parents[3] / "models").resolve()


@dataclass
class ModelArtifact:
    """Bundle that gets pickled to disk for one trained model.

    The ``model`` field is intentionally typed loosely (``Any``) — at
    runtime it's an ``xgboost.XGBClassifier`` but importing xgboost
    at module-load time would force the optional dep on every user.
    Train and predict both validate the duck-typing on use.
    """

    strategy_name: str
    model_version: str  # bumped when feature schema or hyperparams change
    feature_columns: Sequence[str]
    fit_at: datetime
    n_train_rows: int
    n_winners: int  # rows with label == 1
    train_metrics: dict[str, float] = field(default_factory=dict)
    holdout_metrics: dict[str, float] = field(default_factory=dict)
    hyperparams: dict[str, Any] = field(default_factory=dict)
    notes: str = ""
    model: Any = None

    @property
    def base_rate(self) -> float:
        """Fraction of training rows labeled WINNER (helps interpret AUC)."""
        return self.n_winners / self.n_train_rows if self.n_train_rows else 0.0


def model_path(
    strategy_name: str,
    *,
    timestamp: datetime | None = None,
) -> Path:
    """Return the path where an artifact for ``strategy_name`` would live.

    With ``timestamp=None`` returns the ``latest.pkl`` pointer; with
    a timestamp returns the timestamped versioned filename.
    """
    folder = _models_root() / strategy_name
    if timestamp is None:
        return folder / "latest.pkl"
    iso = timestamp.astimezone(UTC).strftime("%Y%m%dT%H%M%S")
    return folder / f"{strategy_name}_{iso}.pkl"


def save_model(artifact: ModelArtifact) -> Path:
    """Pickle ``artifact`` to disk. Writes BOTH the timestamped file
    AND ``latest.pkl`` so on-disk discovery is one path lookup.

    Returns the timestamped filename (the canonical artifact for
    audit / rollback). The ``latest.pkl`` is a duplicate, not a
    symlink — symlinks misbehave on Windows and we don't gain much
    from them on a single-host deployment.
    """
    versioned = model_path(artifact.strategy_name, timestamp=artifact.fit_at)
    versioned.parent.mkdir(parents=True, exist_ok=True)
    with versioned.open("wb") as f:
        pickle.dump(artifact, f)

    latest = model_path(artifact.strategy_name, timestamp=None)
    with latest.open("wb") as f:
        pickle.dump(artifact, f)

    log.info(
        "saved model %s v%s → %s (%d rows, base rate %.1f%%)",
        artifact.strategy_name,
        artifact.model_version,
        versioned,
        artifact.n_train_rows,
        100 * artifact.base_rate,
    )
    return versioned


def load_model(strategy_name: str) -> ModelArtifact | None:
    """Load the latest model for ``strategy_name``, or ``None`` if absent.

    Soft-fails (returns ``None`` and logs a warning) on:
      * the file not existing — first run before any training,
      * pickle corruption — partially-written file mid-train,
      * unpickling raising — Python/xgboost version skew.

    Caller (predict.py / runner) treats ``None`` as "no advisory
    score available" and proceeds without it.
    """
    p = model_path(strategy_name, timestamp=None)
    if not p.exists():
        return None
    try:
        with p.open("rb") as f:
            artifact = pickle.load(f)
    except Exception as exc:
        log.warning("model load failed (%s): %s", p, exc)
        return None
    if not isinstance(artifact, ModelArtifact):
        log.warning("model file at %s does not contain a ModelArtifact", p)
        return None
    return artifact


def list_models() -> list[ModelArtifact]:
    """Return one artifact per strategy directory under the models root.

    Used by the ``stockscan ml status`` CLI for a quick overview.
    Drops directories that don't yet have a ``latest.pkl``.
    """
    root = _models_root()
    if not root.exists():
        return []
    out: list[ModelArtifact] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        artifact = load_model(child.name)
        if artifact is not None:
            out.append(artifact)
    return out
