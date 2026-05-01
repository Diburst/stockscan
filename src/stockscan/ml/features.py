"""Feature engineering for the meta-labeling classifier.

One pure function — :func:`build_features` — takes everything we know
about a signal at the moment it fired (bars up to and including
as_of, the strategy's signal metadata, the contemporaneous regime
row) and returns a flat dict of float features.

Design principles:

  1. **No look-ahead.** Every feature is computable from data
     available at signal time. This is the same invariant the
     strategies and regime composite already enforce.

  2. **Sparse but expressive.** Eleven base features chosen from
     Gu-Kelly-Xiu (2020): a momentum block (5d/21d/63d returns),
     a volatility block (realized vol at three windows), a
     regime block (composite + label one-hot), and a setup-quality
     block (RSI, distance to 52w high, signal score).

  3. **Schema-stable.** :data:`FEATURE_COLUMNS` is the canonical
     ordering; train and predict both serialize through it so an
     accidental column reorder doesn't silently scramble the model.

  4. **NaN-safe.** Missing data (insufficient history, missing
     regime row, absent indicator) is filled with neutral values
     rather than raising — XGBoost handles NaNs natively but
     consistent fills make threshold tuning more interpretable.

If/when we want richer features (sector relative strength, cross-
sectional rank, microstructure-derived intraday features) the right
move is to add them here behind feature flags rather than spawning a
parallel pipeline. Models are versioned (see store.py) so adding new
columns mid-stream just requires re-training.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

import numpy as np

from stockscan.indicators import atr, rsi

if TYPE_CHECKING:
    from datetime import date

    import pandas as pd

    from stockscan.regime.store import MarketRegime


# Canonical feature order. DO NOT reorder mid-version — the trained
# model expects exactly this layout. Add NEW columns by appending
# (and bumping the model version when you re-train).
FEATURE_COLUMNS: tuple[str, ...] = (
    # Momentum block
    "ret_5d",
    "ret_21d",
    "ret_63d",
    # Volatility block (realized, annualised)
    "realized_vol_5d",
    "realized_vol_21d",
    "realized_vol_63d",
    # Setup quality
    "rsi_14",
    "rsi_2",
    "closeness_52w",
    "atr_over_close",
    "signal_score",
    # Regime
    "regime_composite",
    "regime_trending_up",
    "regime_trending_down",
    "regime_choppy",
    "regime_transitioning",
    "credit_stress_flag",
)


# Neutral fills for each feature when the underlying calc isn't
# available (insufficient history, missing regime row, etc.). Keeps
# the row dense and lets XGBoost learn from "NaN" implicitly via the
# value distribution rather than via missingness — easier to debug.
_NEUTRAL_FILLS: dict[str, float] = {
    "ret_5d": 0.0,
    "ret_21d": 0.0,
    "ret_63d": 0.0,
    "realized_vol_5d": 0.20,  # ~20%/yr is the long-run S&P avg
    "realized_vol_21d": 0.20,
    "realized_vol_63d": 0.20,
    "rsi_14": 50.0,
    "rsi_2": 50.0,
    "closeness_52w": 0.85,  # mid-pack
    "atr_over_close": 0.02,  # ~2% ATR is typical for a liquid name
    "signal_score": 0.5,
    "regime_composite": 0.5,
    "regime_trending_up": 0.0,
    "regime_trending_down": 0.0,
    "regime_choppy": 0.0,
    "regime_transitioning": 0.0,
    "credit_stress_flag": 0.0,
}


def build_features(
    bars: pd.DataFrame,
    as_of: date,
    signal_metadata: dict[str, Any] | None = None,
    regime: MarketRegime | None = None,
    *,
    signal_score: float | None = None,
) -> dict[str, float]:
    """Build the feature vector for one signal.

    Parameters
    ----------
    bars:
        DataFrame indexed by tz-aware UTC timestamps with at least
        ``open``, ``high``, ``low``, ``close``, ``volume`` columns.
        Only rows up through ``as_of`` are used; any later rows are
        ignored to preserve the no-look-ahead invariant.
    as_of:
        The signal's ``as_of_date``. Forward-looking features are
        explicitly forbidden from this point onward.
    signal_metadata:
        The strategy's ``RawSignal.metadata`` dict. We pull
        ``closeness_52w`` from here when the strategy has already
        computed it (avoids a duplicate ``rolling.max()`` pass).
    regime:
        Contemporaneous :class:`MarketRegime`. ``None`` is fine —
        regime features fall back to neutral.
    signal_score:
        The strategy's ``RawSignal.score`` as a float. We pass it
        in explicitly rather than re-compute, since the score
        formula varies per strategy.

    Returns
    -------
    dict[str, float]
        Keys exactly == :data:`FEATURE_COLUMNS`. Values are floats;
        no NaNs (replaced with neutral fills).
    """
    md = signal_metadata or {}
    out: dict[str, float] = dict(_NEUTRAL_FILLS)

    # ---- Slice bars to as_of ----
    view = _slice_to_as_of(bars, as_of)
    close = view.get("close")
    if close is None or len(close) < 5:
        # Severely truncated history: regime + signal_score are still
        # useful; bail on the bar-derived features.
        _fill_signal_score(out, signal_score)
        _fill_regime(out, regime)
        return out

    # ---- Setup quality from metadata (when available) ----
    closeness = md.get("closeness_52w")
    if closeness is not None and _is_finite(closeness):
        out["closeness_52w"] = float(closeness)
    else:
        # Compute on the fly if the strategy didn't include it.
        out["closeness_52w"] = _compute_closeness(close, window=252)

    out["signal_score"] = (
        float(signal_score) if signal_score is not None else _NEUTRAL_FILLS["signal_score"]
    )

    # ---- Returns block ----
    last_close = float(close.iloc[-1])
    for label, n in (("ret_5d", 5), ("ret_21d", 21), ("ret_63d", 63)):
        if len(close) > n:
            prev = float(close.iloc[-1 - n])
            if prev > 0:
                out[label] = (last_close / prev) - 1.0

    # ---- Realized vol block (annualised) ----
    log_returns = np.log(close).diff().dropna()
    for label, n in (
        ("realized_vol_5d", 5),
        ("realized_vol_21d", 21),
        ("realized_vol_63d", 63),
    ):
        if len(log_returns) >= n:
            sd = float(log_returns.iloc[-n:].std(ddof=1))
            if _is_finite(sd) and sd > 0:
                out[label] = sd * (252.0**0.5)

    # ---- RSI block ----
    if len(close) >= 30:
        out["rsi_14"] = _last_finite(rsi(close, 14), 50.0)
    if len(close) >= 5:
        out["rsi_2"] = _last_finite(rsi(close, 2), 50.0)

    # ---- ATR / close ratio (relative volatility, scale-free) ----
    high = view.get("high")
    low = view.get("low")
    if high is not None and low is not None and len(close) >= 21:
        atr_v = _last_finite(atr(high, low, close, 14), float("nan"))
        if _is_finite(atr_v) and last_close > 0:
            out["atr_over_close"] = atr_v / last_close

    # ---- Regime ----
    _fill_regime(out, regime)

    # Final scrub — replace any NaN/inf that snuck through with the
    # neutral fill for that column. Cheap insurance.
    for k, v in list(out.items()):
        if not _is_finite(v):
            out[k] = _NEUTRAL_FILLS.get(k, 0.0)

    return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _slice_to_as_of(bars: pd.DataFrame, as_of: date) -> pd.DataFrame:
    """Return rows whose date is ≤ as_of. Tolerates non-datetime indices."""
    idx_dates = bars.index.date if hasattr(bars.index, "date") else None
    if idx_dates is None:
        return bars
    return bars[idx_dates <= as_of]


def _compute_closeness(close: pd.Series, *, window: int = 252) -> float:
    """Today's close as a fraction of the trailing-window max close."""
    if len(close) < 2:
        return _NEUTRAL_FILLS["closeness_52w"]
    eff_window = min(window, len(close))
    max_c = float(close.iloc[-eff_window:].max())
    if max_c <= 0:
        return _NEUTRAL_FILLS["closeness_52w"]
    return float(close.iloc[-1]) / max_c


def _fill_regime(out: dict[str, float], regime: MarketRegime | None) -> None:
    if regime is None:
        return
    composite = getattr(regime, "composite_score", None)
    if composite is not None:
        with contextlib.suppress(TypeError, ValueError):
            out["regime_composite"] = float(composite)
    label = getattr(regime, "regime", None)
    if label:
        col = f"regime_{label}"
        if col in out:
            out[col] = 1.0
    if getattr(regime, "credit_stress_flag", False):
        out["credit_stress_flag"] = 1.0


def _fill_signal_score(out: dict[str, float], signal_score: float | None) -> None:
    if signal_score is not None and _is_finite(signal_score):
        out["signal_score"] = float(signal_score)


def _last_finite(series: pd.Series, default: float) -> float:
    if len(series) == 0:
        return default
    val = float(series.iloc[-1])
    return val if _is_finite(val) else default


def _is_finite(v: object) -> bool:
    try:
        return np.isfinite(float(v))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
