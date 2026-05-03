"""SPY drawdown + days-since-correction.

Two state variables, one little dataclass — both computed by walking
SPY's close history backwards from ``as_of``.

  * Current drawdown from the trailing all-time high — % below ATH +
    days since the ATH was made.
  * Days since the most recent 5%, 10%, and 20% correction — defined
    as the most recent date on which SPY closed >=N% below the
    contemporaneous trailing ATH.

These don't predict anything; they answer "where are we in the cycle?".
The historical median gap between 10% corrections is ~290 trading
days — a current gap of 600 days is in the 90th percentile of "overdue."
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import date as _date

    import pandas as pd


@dataclass(frozen=True, slots=True)
class CorrectionGap:
    """Days since the most recent close at-least-X% below trailing ATH."""

    threshold_pct: float  # 5.0, 10.0, 20.0
    available: bool
    days_since: int | None
    last_correction_date: _date | None


@dataclass(frozen=True, slots=True)
class DrawdownState:
    available: bool
    last_close: float | None
    ath_close: float | None
    ath_date: _date | None
    drawdown_pct: float | None  # negative when below ATH; 0 when at ATH
    days_since_ath: int | None
    correction_5pct: CorrectionGap
    correction_10pct: CorrectionGap
    correction_20pct: CorrectionGap

    @classmethod
    def unavailable(cls) -> DrawdownState:
        return cls(
            available=False,
            last_close=None,
            ath_close=None,
            ath_date=None,
            drawdown_pct=None,
            days_since_ath=None,
            correction_5pct=CorrectionGap(5.0, False, None, None),
            correction_10pct=CorrectionGap(10.0, False, None, None),
            correction_20pct=CorrectionGap(20.0, False, None, None),
        )


def compute_drawdown_state(
    spy_bars: pd.DataFrame | None,
    as_of: _date,
) -> DrawdownState:
    """Walk SPY's history to compute drawdown + correction gaps."""
    import pandas as pd  # lazy

    if spy_bars is None or spy_bars.empty or "close" not in spy_bars.columns:
        return DrawdownState.unavailable()

    df = spy_bars.copy()
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index, utc=True)
    closes = df["close"].sort_index()
    if closes.empty:
        return DrawdownState.unavailable()

    last_close = float(closes.iloc[-1])
    last_date = closes.index[-1].date()

    # All-time high so far (in our window).
    ath_close = float(closes.max())
    ath_idx = closes.idxmax()
    ath_date = ath_idx.date()

    drawdown_pct = float((last_close / ath_close - 1.0) * 100) if ath_close > 0 else None
    days_since_ath = (last_date - ath_date).days

    # Days since each correction threshold.
    correction_5 = _correction_gap(closes, 5.0)
    correction_10 = _correction_gap(closes, 10.0)
    correction_20 = _correction_gap(closes, 20.0)

    return DrawdownState(
        available=True,
        last_close=last_close,
        ath_close=ath_close,
        ath_date=ath_date,
        drawdown_pct=drawdown_pct,
        days_since_ath=days_since_ath,
        correction_5pct=correction_5,
        correction_10pct=correction_10,
        correction_20pct=correction_20,
    )


def _correction_gap(closes, threshold_pct: float) -> CorrectionGap:
    """Walk backwards through ``closes`` and find the most recent bar
    whose close was >= ``threshold_pct`` below the contemporaneous
    trailing ATH.

    Returns the gap in calendar days. We use calendar days (not
    trading days) so the number is intuitive against typical
    "X days since the last correction" framing.
    """
    import pandas as pd  # noqa: F401 — used implicitly by closes accessors

    cum_max = closes.cummax()
    drawdowns = (closes / cum_max - 1.0) * 100  # negative when below ATH
    # Bars where drawdown was AT LEAST threshold_pct below.
    breached = drawdowns[drawdowns <= -threshold_pct]
    if breached.empty:
        # Either insufficient history or never breached — return
        # ``available=True`` with ``days_since=None`` to convey "no
        # correction at this threshold in our history".
        return CorrectionGap(threshold_pct, True, None, None)
    last_breach_idx = breached.index[-1]
    last_breach_date = last_breach_idx.date()
    last_close_date = closes.index[-1].date()
    return CorrectionGap(
        threshold_pct=threshold_pct,
        available=True,
        days_since=(last_close_date - last_breach_date).days,
        last_correction_date=last_breach_date,
    )
