"""Pure regime rules — the market-health controls, as functions of Series.

Two controls, deliberately separate, because the evidence supports them for
different jobs and with different signs across strategy families:

**Trend gate** — SPY above / below its 200-day SMA, with a dwell so a single
close through the line does not flip the gate. The gate governs *new
entries only*: closed → no new longs; open positions run their own exits.
This is the Faber / Alpha Architect / Clenow index filter, whose evidence
is drawdown reduction (Faber 2007; ap Gwilym et al. 2010), not return
prediction (Zakamulin). Daily evaluation without a band is the version
practitioners warn against, hence the dwell.

**Volatility scalar** — realized SPY volatility, percentile-ranked over a
trailing year. In the top tercile the scalar shrinks position size toward
``TARGET_VOL / realized`` (Moreira & Muir 2017; Barroso & Santa-Clara 2015;
Harvey et al. 2018), with a dead band below that (Bongaerts et al. 2020).
It never scales *up*: a long-only retail book does not lever into calm.
Each strategy declares whether the scalar applies to it — momentum sizes
down in high vol because that is where momentum crashes; mean reversion
does not, because reversal profits rise with volatility (Nagel 2012).

**Credit-stress flag** — a rare tail override on HY OAS (top 15% of the
trailing year AND rising over 5 observations). Blocks new longs while it
fires. Kept as a breaker only; credit level is not a swing-horizon signal.

No look-ahead: every computation is a trailing window or a forward state
machine over the series. Recomputing on a truncated copy of the input
matches the live value at the truncation point — ``tests/test_regime_rules.py``
holds the property test.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# ---- Trend gate ------------------------------------------------------------
TREND_SMA_WINDOW = 200
# Consecutive closes on the opposite side of the SMA before the gate flips.
TREND_DWELL = 3

# ---- Volatility scalar -----------------------------------------------------
VOL_WINDOW = 20  # bars of daily returns in the realized-vol estimate
VOL_RANK_WINDOW = 252  # trailing year for the percentile rank
VOL_HIGH_RANK = 2.0 / 3.0  # top tercile → scaling active
TARGET_VOL = 0.16  # annualized; roughly SPY's long-run realized vol
VOL_SCALAR_FLOOR = 0.5

# ---- Credit-stress flag ----------------------------------------------------
CREDIT_RANK_WINDOW = 252
CREDIT_STRESS_RANK_THRESHOLD = 0.85
CREDIT_STRESS_LOOKBACK = 5

_TRADING_DAYS_PER_YEAR = 252


def trend_gate(
    close: pd.Series,
    sma: pd.Series,
    *,
    dwell: int = TREND_DWELL,
) -> tuple[pd.Series, pd.Series]:
    """Gate state per bar, with hysteresis.

    Returns ``(open, days_on_side)``:

    * ``open`` — ``True`` while the gate is open. The gate opens after
      ``dwell`` consecutive closes above the SMA and closes after ``dwell``
      consecutive closes below it. Bars before the SMA exists are ``False``.
    * ``days_on_side`` — how many consecutive closes the market has spent on
      its current side of the SMA (the dwell counter), for the dashboard.

    The first valid bar seeds the state from its own side, so a series that
    begins in an uptrend starts open.
    """
    c = close.to_numpy(dtype=float)
    m = sma.to_numpy(dtype=float)
    n = len(c)
    is_open = np.zeros(n, dtype=bool)
    run = np.zeros(n, dtype=int)

    state: bool | None = None
    side: bool | None = None  # True = above SMA
    count = 0
    for i in range(n):
        if np.isnan(m[i]) or np.isnan(c[i]):
            continue
        above = c[i] > m[i]
        if side is None:
            state = above
            side = above
            count = 1
        elif above == side:
            count += 1
        else:
            side = above
            count = 1
        if state != side and count >= dwell:
            state = side
        is_open[i] = bool(state)
        run[i] = count

    return (
        pd.Series(is_open, index=close.index, name="trend_gate_open"),
        pd.Series(run, index=close.index, name="days_on_side"),
    )


def realized_vol(close: pd.Series, *, window: int = VOL_WINDOW) -> pd.Series:
    """Annualized standard deviation of daily log returns over ``window`` bars."""
    log_ret = np.log(close / close.shift(1))
    rv = log_ret.rolling(window=window, min_periods=window).std(ddof=0)
    return (rv * np.sqrt(_TRADING_DAYS_PER_YEAR)).rename("realized_vol")


def vol_pct_rank(rv: pd.Series, *, window: int = VOL_RANK_WINDOW) -> pd.Series:
    """Trailing percentile rank of realized vol (1.0 = highest in the window)."""
    return rv.rolling(window=window, min_periods=window).rank(pct=True).rename("vol_pct_rank")


def vol_scalar(
    rv: pd.Series,
    rank: pd.Series,
    *,
    target: float = TARGET_VOL,
    high_rank: float = VOL_HIGH_RANK,
    floor: float = VOL_SCALAR_FLOOR,
) -> pd.Series:
    """Position-size multiplier in ``[floor, 1.0]``.

    ``target / realized`` clipped to ``[floor, 1.0]`` when the vol rank is in
    the top tercile; ``1.0`` otherwise (the dead band). NaN where the inputs
    are not yet available.
    """
    scaled = (target / rv).clip(lower=floor, upper=1.0)
    active = rank >= high_rank
    out = pd.Series(1.0, index=rv.index, dtype=float)
    out[active] = scaled[active]
    out[rv.isna() | rank.isna()] = np.nan
    return out.rename("vol_scalar")


def credit_stress_flag(
    hy_oas: pd.Series,
    *,
    window: int = CREDIT_RANK_WINDOW,
    rank_threshold: float = CREDIT_STRESS_RANK_THRESHOLD,
    lookback: int = CREDIT_STRESS_LOOKBACK,
) -> pd.Series:
    """``True`` when HY OAS is in the top ``1 - rank_threshold`` of its
    trailing window AND higher than ``lookback`` observations ago.

    Warmup bars return ``False`` so the result is a clean boolean mask.
    """
    rank = hy_oas.rolling(window=window, min_periods=window).rank(pct=True)
    rising = hy_oas > hy_oas.shift(lookback)
    flag = (rank > rank_threshold) & rising
    return flag.fillna(False).astype(bool).rename("credit_stress_flag")


def hy_oas_pct_rank(hy_oas: pd.Series, *, window: int = CREDIT_RANK_WINDOW) -> pd.Series:
    """Trailing percentile rank of HY OAS, for the dashboard."""
    return hy_oas.rolling(window=window, min_periods=window).rank(pct=True).rename("hy_oas_pct_rank")


def regime_frame(spy_close: pd.Series, hy_oas: pd.Series | None) -> pd.DataFrame:
    """Every control, per bar of ``spy_close``, in one frame.

    Columns: ``sma200``, ``sma200_slope_20d``, ``trend_gate_open``,
    ``days_on_side``, ``realized_vol``, ``vol_pct_rank``, ``vol_scalar``,
    ``hy_oas``, ``hy_oas_pct_rank``, ``credit_stress_flag``. HY OAS is
    forward-filled onto the SPY calendar (FRED publishes on business days
    with a lag); when ``hy_oas`` is None the credit columns are NaN/False.

    The live detector reads the last row; the backtest engine reads the
    whole frame once per run, so both paths apply identical rules.
    """
    close = spy_close.astype(float)
    sma200 = close.rolling(TREND_SMA_WINDOW, min_periods=TREND_SMA_WINDOW).mean()
    lagged = sma200.shift(20)
    slope = (sma200 - lagged) / lagged
    gate_open, days_on_side = trend_gate(close, sma200)
    rv = realized_vol(close)
    rank = vol_pct_rank(rv)
    scalar = vol_scalar(rv, rank)

    frame = pd.DataFrame(
        {
            "sma200": sma200,
            "sma200_slope_20d": slope,
            "trend_gate_open": gate_open,
            "days_on_side": days_on_side,
            "realized_vol": rv,
            "vol_pct_rank": rank,
            "vol_scalar": scalar,
        }
    )
    if hy_oas is not None and not hy_oas.empty:
        oas = hy_oas.astype(float)
        # Align on calendar date: SPY bars are UTC close timestamps, FRED
        # observations are dates. Each SPY bar takes the latest OAS print on
        # or before its date.
        bar_dates = pd.DatetimeIndex(pd.to_datetime(frame.index).date)
        oas_dates = pd.DatetimeIndex(pd.to_datetime(oas.index).date)
        stress = credit_stress_flag(oas)
        oas_rank = hy_oas_pct_rank(oas)

        def _on_bars(series: pd.Series) -> pd.Series:
            aligned = pd.Series(series.to_numpy(), index=oas_dates).reindex(
                bar_dates, method="ffill"
            )
            return pd.Series(aligned.to_numpy(), index=frame.index)

        frame["hy_oas"] = _on_bars(oas)
        frame["hy_oas_pct_rank"] = _on_bars(oas_rank)
        frame["credit_stress_flag"] = _on_bars(stress).fillna(False).astype(bool)
    else:
        frame["hy_oas"] = np.nan
        frame["hy_oas_pct_rank"] = np.nan
        frame["credit_stress_flag"] = False
    return frame
