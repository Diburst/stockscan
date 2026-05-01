"""Meta-labeling layer (Lopez de Prado, 2017).

Architecture: stockscan's primary models are the strategies in
:mod:`stockscan.strategies` — they answer "is this a setup?" and
optimize for recall. This module wraps them with a binary classifier
that answers "given a setup of this shape in this regime, what's the
probability it actually pays off?" — the meta-label.

The score is **purely advisory**. It gets persisted into
``signals.metadata.meta_label_proba`` so the dashboard can render it
and so future runner changes can promote it from advisory to a hard
filter once enough live data has accumulated to validate a threshold.
The current FilterChain integration **never rejects** on the meta-
label score; it just attaches it.

Module surface, in dependency order:

  * ``features``  — pure function building a feature vector from
                    bars + signal metadata + regime row.
  * ``labels``    — triple-barrier labeling for historical signals.
  * ``store``     — on-disk model artifacts under ./models/<strategy>/.
  * ``train``     — assemble training data + fit XGBoost binary
                    classifier + dump model.
  * ``predict``   — load model lazily + score one signal.

Optional dependency: xgboost + scikit-learn (under the ``[ml]`` extra).
Importing this package is safe without them — the import-error is
deferred until you actually call train() or predict().
"""

from __future__ import annotations

from stockscan.ml.features import FEATURE_COLUMNS, build_features
from stockscan.ml.labels import TripleBarrierLabel, triple_barrier_label
from stockscan.ml.predict import score_signal
from stockscan.ml.store import (
    ModelArtifact,
    list_models,
    load_model,
    model_path,
    save_model,
)
from stockscan.ml.train import TrainResult, train_model

__all__ = [
    "FEATURE_COLUMNS",
    "ModelArtifact",
    "TrainResult",
    "TripleBarrierLabel",
    "build_features",
    "list_models",
    "load_model",
    "model_path",
    "save_model",
    "score_signal",
    "train_model",
    "triple_barrier_label",
]
