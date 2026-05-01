"""Training pipeline for the meta-labeling classifier.

End-to-end:

  1. Pull every historical signal for ``strategy_name`` from the DB.
  2. For each one, slice the local bars store to the as_of_date and
     extract a feature vector via :func:`build_features`.
  3. Slice forward 20 trading days and assign a triple-barrier label.
  4. Drop any rows where the forward window was incomplete (recent
     signals with too few subsequent bars).
  5. Time-series train/test split — chronological, NOT shuffled,
     because look-ahead leakage is the cardinal sin in this domain.
     Default: oldest 80% train, newest 20% holdout.
  6. Fit ``XGBClassifier`` with conservative hyperparams (max_depth=4,
     n_estimators=200, learning_rate=0.05). These are deliberately
     boring — we don't have enough data per strategy to support a
     hyperparameter sweep without overfitting to the holdout.
  7. Compute holdout AUC + base rate + a few thresholded metrics
     (precision/recall at p>0.55) so the user can see whether the
     model has any edge before shipping it.
  8. Pickle a :class:`ModelArtifact` to ``./models/<strategy>/``.

Optional dependencies (xgboost, scikit-learn) are imported INSIDE
the function so that:
  * ``import stockscan.ml`` keeps working without them,
  * a missing dep raises a clear actionable error at train time
    (``stockscan ml train`` tells the user to ``uv sync --extra ml``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pandas as pd
from sqlalchemy import text

from stockscan.data.store import get_bars
from stockscan.db import session_scope
from stockscan.ml.features import FEATURE_COLUMNS, build_features
from stockscan.ml.labels import (
    TripleBarrierLabel,
    select_forward_bars,
    triple_barrier_label,
)
from stockscan.ml.store import ModelArtifact, save_model
from stockscan.regime import latest_regime as latest_regime_for

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

log = logging.getLogger(__name__)


# Conservative defaults. These ARE NOT deeply tuned — they're just
# safe-by-default values for a small training set. The hyperparam
# optimization story is on the roadmap (TODO §strategy optimizer).
_DEFAULT_HYPERPARAMS: dict[str, Any] = {
    "max_depth": 4,
    "n_estimators": 200,
    "learning_rate": 0.05,
    "subsample": 0.8,
    "colsample_bytree": 0.8,
    "min_child_weight": 5,
    "reg_lambda": 1.0,
    "objective": "binary:logistic",
    "eval_metric": "auc",
}


@dataclass(frozen=True, slots=True)
class TrainResult:
    """Returned to the CLI / caller after a successful training run."""

    strategy_name: str
    model_version: str
    n_signals_seen: int  # rows pulled from the DB before any filtering
    n_train_rows: int  # rows that survived label + feature build
    n_holdout_rows: int
    train_auc: float
    holdout_auc: float
    base_rate: float
    artifact_path: str

    @property
    def usable(self) -> bool:
        """Heuristic: holdout AUC > 0.55 indicates non-trivial signal."""
        return self.holdout_auc > 0.55


def train_model(
    strategy_name: str,
    *,
    model_version: str = "1.0.0",
    strategy_version: str | None = None,
    holding_days: int = 20,
    profit_take_atr_mult: float = 2.0,
    holdout_fraction: float = 0.2,
    min_rows: int = 100,
    hyperparams: dict[str, Any] | None = None,
    session: Session | None = None,
) -> TrainResult:
    """Build training data, fit the classifier, persist the artifact.

    Parameters
    ----------
    strategy_name:
        The strategy whose historical signals we train on. One model
        per strategy — different strategies have very different
        signal-generating distributions.
    model_version:
        Stamped into the artifact. Bump when feature schema or
        hyperparams change so the loader can fail loudly on skew.
    strategy_version:
        If supplied, restrict training data to signals produced by
        exactly this strategy version. ``None`` (the default) means
        "current registered version" — looked up from
        :data:`STRATEGY_REGISTRY`. Pass an explicit version string
        when re-training a model on a historical strategy version.
    holding_days:
        Triple-barrier max holding window. 20 trading days ≈ 1
        calendar month is standard.
    profit_take_atr_mult:
        Profit-take barrier in ATR units.
    holdout_fraction:
        Newest fraction held out as a chronological test set. 0.2
        is a safe default for a small training set.
    min_rows:
        Minimum total rows after labeling required to attempt a fit.
        Below this we raise rather than ship a model with no
        statistical power.
    hyperparams:
        XGBoost hyperparameter overrides; merged onto _DEFAULT_HYPERPARAMS.
    session:
        Optional caller-managed SQLAlchemy session. When ``None`` we
        open + close one ourselves.

    Raises
    ------
    RuntimeError
        If xgboost / scikit-learn aren't installed (with a clear
        message pointing at ``uv sync --extra ml``), or if there
        aren't enough labeled rows to fit a model.
    """
    # Defer imports — keeps `import stockscan.ml` working without xgboost.
    try:
        from sklearn.metrics import (  # type: ignore[import-not-found]
            precision_score,
            recall_score,
            roc_auc_score,
        )
        from xgboost import XGBClassifier  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError(
            "Meta-labeling requires xgboost and scikit-learn. "
            "Install with `uv sync --extra ml` (or `pip install -e .[ml]`)."
        ) from exc

    started = datetime.now(UTC)

    # Resolve the strategy_version filter. Default = current registered
    # version (look up via STRATEGY_REGISTRY). Caller can override with
    # an explicit version to re-train on historical data.
    if strategy_version is None:
        from stockscan.strategies import STRATEGY_REGISTRY, discover_strategies
        discover_strategies()
        try:
            strategy_version = STRATEGY_REGISTRY.get(strategy_name).version
        except KeyError as exc:
            raise RuntimeError(
                f"Strategy {strategy_name!r} is not registered. "
                f"Cannot resolve current strategy_version for training."
            ) from exc

    if session is None:
        with session_scope() as s:
            return _train_in_session(
                s,
                started=started,
                strategy_name=strategy_name,
                strategy_version=strategy_version,
                model_version=model_version,
                holding_days=holding_days,
                profit_take_atr_mult=profit_take_atr_mult,
                holdout_fraction=holdout_fraction,
                min_rows=min_rows,
                hyperparams=hyperparams,
                xgb_classifier_cls=XGBClassifier,
                roc_auc_score=roc_auc_score,
                precision_score=precision_score,
                recall_score=recall_score,
            )
    return _train_in_session(
        session,
        started=started,
        strategy_name=strategy_name,
        strategy_version=strategy_version,
        model_version=model_version,
        holding_days=holding_days,
        profit_take_atr_mult=profit_take_atr_mult,
        holdout_fraction=holdout_fraction,
        min_rows=min_rows,
        hyperparams=hyperparams,
        xgb_classifier_cls=XGBClassifier,
        roc_auc_score=roc_auc_score,
        precision_score=precision_score,
        recall_score=recall_score,
    )


def _train_in_session(
    s: Session,
    *,
    started: datetime,
    strategy_name: str,
    strategy_version: str,
    model_version: str,
    holding_days: int,
    profit_take_atr_mult: float,
    holdout_fraction: float,
    min_rows: int,
    hyperparams: dict[str, Any] | None,
    xgb_classifier_cls: Any,
    roc_auc_score: Any,
    precision_score: Any,
    recall_score: Any,
) -> TrainResult:
    # 1. Pull historical signals for the requested (name, version).
    #    Filtering by strategy_version means re-training on the same
    #    strategy after a version bump uses ONLY the new-version
    #    signals — not a mix of v1.0 + v1.1, which would silently
    #    contaminate the training distribution.
    rows = s.execute(
        text(
            """
            SELECT signal_id, symbol, score, as_of_date,
                   suggested_entry, suggested_stop, metadata, status
            FROM signals
            WHERE strategy_name = :n
              AND strategy_version = :v
              AND suggested_entry IS NOT NULL
              AND suggested_stop IS NOT NULL
            ORDER BY as_of_date ASC, signal_id ASC
            """
        ),
        {"n": strategy_name, "v": strategy_version},
    ).all()
    n_signals_seen = len(rows)
    if n_signals_seen == 0:
        raise RuntimeError(
            f"No historical signals found for strategy {strategy_name!r} "
            f"version {strategy_version!r}. "
            f"Run `stockscan signals backfill {strategy_name}` to populate "
            f"the signals table with the current strategy version."
        )

    # 2. + 3. Build features + labels.
    feature_rows: list[dict[str, float]] = []
    labels: list[int] = []
    asof_dates: list[Any] = []
    dropped_no_bars = 0
    dropped_short_window = 0

    for row in rows:
        symbol = row.symbol
        as_of = row.as_of_date
        # Pull a wide bar window: enough history for features (1y+) AND
        # enough forward bars for the label (holding_days).
        try:
            bars = get_bars(
                symbol,
                start=_year_before(as_of, years=2),
                end=_days_after(as_of, days=holding_days * 2),
                session=s,
            )
        except Exception as exc:
            log.debug("train: get_bars failed for %s: %s", symbol, exc)
            dropped_no_bars += 1
            continue

        if bars is None or bars.empty:
            dropped_no_bars += 1
            continue

        # Label first — if we can't label, skip features too.
        forward = select_forward_bars(bars, as_of, max_days=holding_days)
        if len(forward) < holding_days // 2:
            # Less than half the holding window available — too short
            # to trust the label.
            dropped_short_window += 1
            continue

        atr_at_entry = (row.metadata or {}).get("atr") if row.metadata else None
        label = triple_barrier_label(
            forward,
            entry=row.suggested_entry,
            stop=row.suggested_stop,
            atr_at_entry=atr_at_entry,
            profit_take_atr_mult=profit_take_atr_mult,
            max_days=holding_days,
        )
        if label is None:
            dropped_short_window += 1
            continue

        # Features. Pull the regime row at as_of for the regime block.
        regime = _safe_regime(s, as_of)
        score_value = float(row.score) if row.score is not None else None
        features = build_features(
            bars=bars,
            as_of=as_of,
            signal_metadata=row.metadata or {},
            regime=regime,
            signal_score=score_value,
        )
        feature_rows.append(features)
        labels.append(int(label))
        asof_dates.append(as_of)

    n_rows = len(labels)
    log.info(
        "train: %s — pulled %d signals, kept %d (%d no-bars, %d short-window)",
        strategy_name,
        n_signals_seen,
        n_rows,
        dropped_no_bars,
        dropped_short_window,
    )
    if n_rows < min_rows:
        raise RuntimeError(
            f"Only {n_rows} usable rows after labeling — need at least "
            f"{min_rows}. Either widen the backtest window or lower min_rows."
        )

    # 4. Build the DataFrame in canonical column order. Lower-case
    # variables (rather than the sklearn-conventional ``X`` /
    # ``X_train``) to satisfy the project's pep8-naming rules.
    x_all = pd.DataFrame(feature_rows, columns=list(FEATURE_COLUMNS))
    y = pd.Series(labels, name="label")

    # 5. Chronological train/test split.
    split_idx = int(n_rows * (1 - holdout_fraction))
    x_train, x_holdout = x_all.iloc[:split_idx], x_all.iloc[split_idx:]
    y_train, y_holdout = y.iloc[:split_idx], y.iloc[split_idx:]
    n_winners = int(y_train.sum())

    # 6. Fit.
    hp = {**_DEFAULT_HYPERPARAMS, **(hyperparams or {})}
    # Class-weight: scale_pos_weight balances the dataset when the
    # base rate is far from 50%.
    base_rate_train = y_train.mean() if len(y_train) else 0.5
    if 0 < base_rate_train < 1:
        hp.setdefault("scale_pos_weight", (1 - base_rate_train) / base_rate_train)

    model = xgb_classifier_cls(**hp)
    model.fit(x_train, y_train)

    # 7. Metrics.
    train_proba = model.predict_proba(x_train)[:, 1]
    holdout_proba = model.predict_proba(x_holdout)[:, 1] if len(x_holdout) else None
    train_auc = (
        float(roc_auc_score(y_train, train_proba)) if len(set(y_train)) > 1 else 0.5
    )
    holdout_auc = (
        float(roc_auc_score(y_holdout, holdout_proba))
        if holdout_proba is not None and len(set(y_holdout)) > 1
        else 0.5
    )
    holdout_metrics: dict[str, float] = {"auc": holdout_auc}
    if holdout_proba is not None and len(set(y_holdout)) > 1:
        threshold = 0.55
        preds = (holdout_proba >= threshold).astype(int)
        holdout_metrics.update(
            {
                f"precision_at_{threshold:.2f}": float(
                    precision_score(y_holdout, preds, zero_division=0)
                ),
                f"recall_at_{threshold:.2f}": float(
                    recall_score(y_holdout, preds, zero_division=0)
                ),
            }
        )

    log.info(
        "train: %s — train AUC %.3f, holdout AUC %.3f (n=%d / %d, base rate %.1f%%)",
        strategy_name,
        train_auc,
        holdout_auc,
        len(x_train),
        len(x_holdout),
        100 * base_rate_train,
    )

    # 8. Pickle the artifact.
    artifact = ModelArtifact(
        strategy_name=strategy_name,
        model_version=model_version,
        feature_columns=list(FEATURE_COLUMNS),
        fit_at=started,
        n_train_rows=len(x_train),
        n_winners=n_winners,
        train_metrics={"auc": train_auc, "base_rate": float(base_rate_train)},
        holdout_metrics=holdout_metrics,
        hyperparams=hp,
        notes=(
            f"strategy_version={strategy_version}, holding_days={holding_days}, "
            f"profit_take_atr_mult={profit_take_atr_mult}, "
            f"holdout_fraction={holdout_fraction}, min_rows={min_rows}"
        ),
        model=model,
    )
    artifact_path = save_model(artifact)

    return TrainResult(
        strategy_name=strategy_name,
        model_version=model_version,
        n_signals_seen=n_signals_seen,
        n_train_rows=len(x_train),
        n_holdout_rows=len(x_holdout),
        train_auc=train_auc,
        holdout_auc=holdout_auc,
        base_rate=float(base_rate_train),
        artifact_path=str(artifact_path),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_regime(s: Session, as_of: Any) -> Any:
    """Best-effort regime row at ``as_of``. Soft-fails to None.

    Uses ``latest_regime(session=s)`` — current implementation
    returns the most-recent row. For training over a long window
    the ideal would be a per-as_of regime fetch, but that requires
    a different store helper than is currently exposed; defer until
    we have a richer regime-store API.
    """
    try:
        return latest_regime_for(session=s)
    except Exception as exc:  # never fail training on regime
        log.debug("train: regime lookup failed: %s", exc)
        return None


def _year_before(d: Any, *, years: int = 2) -> Any:
    """Subtract N years from a date, tolerating Feb 29."""
    from datetime import date as _date

    if isinstance(d, _date):
        try:
            return d.replace(year=d.year - years)
        except ValueError:
            # Feb 29 → Feb 28
            return d.replace(month=2, day=28, year=d.year - years)
    return d


def _days_after(d: Any, *, days: int) -> Any:
    from datetime import date as _date
    from datetime import timedelta as _td

    if isinstance(d, _date):
        return d + _td(days=days)
    return d


# Re-export the label enum for completeness — train and predict
# both refer to it via stockscan.ml.
__all__ = ["TrainResult", "TripleBarrierLabel", "train_model"]
