"""Typed dataclasses for the per-symbol technical analysis result.

Each sub-state carries an ``available`` flag so the orchestrator can
soft-fail individual sections without blanking out the whole bundle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import date as _date


@dataclass(frozen=True, slots=True)
class ExpectedRange:
    """Forward-projected price range at one horizon, ±1sigma from current price.

    Computed from realized vol (annualised) projected forward by
    ``sqrt(horizon_days / 252)``. Two horizons by default: 7 and 30
    trading days.

    These are realized-vol-derived estimates, NOT option-implied
    move calculations - we don't have option chain data wired up.
    Treat as "expected range based on this stock's recent volatility"
    rather than "the options market's expectation".
    """

    horizon_days: int  # trading days (7, 30)
    sigma_pct: float  # 1-stddev as % of current price
    low: float  # current price * (1 - sigma_pct)
    high: float  # current price * (1 + sigma_pct)
    sigma_dollars: float  # absolute dollars (= current * sigma_pct / 100)


@dataclass(frozen=True, slots=True)
class TrendState:
    available: bool
    bucket: str  # 'strong_up' | 'up' | 'neutral' | 'down' | 'strong_down' | '?'
    label: str  # human-readable
    explanation: str
    # Returns over multiple windows, in % (e.g. 5.2 = +5.2%)
    return_5d: float | None
    return_21d: float | None
    return_63d: float | None
    # MA stack - does close > SMA(20) > SMA(50) > SMA(200)? Higher = more aligned.
    ma_alignment: str  # 'aligned_bullish' | 'aligned_bearish' | 'mixed'
    sma_20: float | None
    sma_50: float | None
    sma_200: float | None
    # Distance of close from each MA, in % (signed; positive = above MA)
    pct_above_sma20: float | None
    pct_above_sma50: float | None
    pct_above_sma200: float | None
    # Exponential moving averages keyed by period (e.g. {9: 151.2, 50: ...}).
    # Periods are configured in trend.py (_EMA_PERIODS); a missing/NaN value
    # is stored as None. Used by the options-context strike-confluence check.
    emas: dict[int, float | None] = field(default_factory=dict)

    @classmethod
    def unavailable(cls) -> TrendState:
        return cls(
            available=False, bucket="?", label="n/a",
            explanation="Insufficient bars to assess trend.",
            return_5d=None, return_21d=None, return_63d=None,
            ma_alignment="mixed",
            sma_20=None, sma_50=None, sma_200=None,
            pct_above_sma20=None, pct_above_sma50=None,
            pct_above_sma200=None, emas={},
        )


@dataclass(frozen=True, slots=True)
class VolatilityState:
    available: bool
    realized_vol_21d_pct: float | None  # annualised, in %
    realized_vol_63d_pct: float | None
    atr_14: float | None  # in dollars
    atr_pct_of_price: float | None  # ATR as % of current price
    hv_percentile: float | None  # 0-100; current 21d realized vol's rank in 252-day distribution
    expected_7d: ExpectedRange | None
    expected_30d: ExpectedRange | None
    bucket: str  # 'low' | 'normal' | 'elevated' | 'high' | '?'
    label: str
    explanation: str
    # Forward vol estimate: EWMA-weighted (λ=0.94) Yang-Zhang annualised vol,
    # in %. More responsive than the trailing-window HV — it drives the
    # expected-move bands and the Black-Scholes strike solver so the two
    # always agree. None when bars/OHLC are insufficient.
    ewma_vol_pct: float | None = None

    @classmethod
    def unavailable(cls) -> VolatilityState:
        return cls(
            available=False, realized_vol_21d_pct=None,
            realized_vol_63d_pct=None, atr_14=None,
            atr_pct_of_price=None,
            hv_percentile=None, expected_7d=None, expected_30d=None,
            bucket="?", label="n/a",
            explanation="Insufficient bars to compute volatility metrics.",
            ewma_vol_pct=None,
        )


@dataclass(frozen=True, slots=True)
class OptionStrike:
    """A Black-Scholes-derived strike suggestion at a target delta + expiry.

    Produced by :mod:`stockscan.analysis.black_scholes` and carried on the
    :class:`OptionsContext` so the analysis page can show a concrete strike
    instead of only a vague "sell premium" hint.

    The vol fed into the model is **realized** HV (21-day), not option-
    implied vol - we have no chain. ``vol_pct`` and ``rate_pct`` record the
    assumptions so the UI can be honest about the model inputs. ``delta`` is
    the realised delta at the solved strike (≈ the target, modulo rounding);
    ``theta`` is per calendar day and ``vega`` is per 1 vol point.
    """

    kind: str  # 'call' | 'put'
    strike: float
    pct_otm: float  # signed % distance of strike from spot (positive = above)
    target_delta: float  # signed delta requested (call +, put −)
    delta: float  # realised BS delta at the solved strike
    price: float  # BS fair value per share
    theta: float  # per calendar day
    vega: float  # per 1 vol point (1%)
    gamma: float
    days_to_expiry: int
    vol_pct: float  # annualised realized vol used (percent)
    rate_pct: float  # annualised risk-free rate used (percent)
    # Structural confluence: short prose strings flagging when this strike
    # sits within 0.5×ATR(14) of a key EMA. Empty when the strike lands in
    # open space. Populated by options_context.
    confluences: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class StrikeSet:
    """The Black-Scholes put + call suggestion for one expiry tenor.

    A tenor is a (days-to-expiry, target-delta) pair — e.g. 6-day Δ0.15
    for "this Friday", 30-day Δ0.20 for "about a month out". The tenors
    themselves are configured in :mod:`stockscan.analysis.options_context`
    (``_STRIKE_TENORS``). ``expiry_date`` is the first Friday at-or-after
    ``as_of + tenor days`` (listed weekly expiries) and ``days_to_expiry``
    is the calendar distance to that Friday, which is also the DTE the legs
    are priced off. ``label`` is the human header shown on the card. Only
    ``sets[0]`` reaches the proposal engine.
    """

    days_to_expiry: int
    target_delta: float  # magnitude (0.15, 0.20); legs carry the signed value
    expiry_date: _date | None
    label: str
    call: OptionStrike | None
    put: OptionStrike | None


@dataclass(frozen=True, slots=True)
class OptionsContext:
    """Options-trading-flavored framing of the technicals.

    NOT a substitute for actual option chain data - this is the
    closest we can get with daily bars + earnings calendar alone.
    When option chain integration ships, the IV percentile + implied
    move will replace the realized-vol approximations here, and the
    Black-Scholes strikes below can switch their σ input from realized
    HV to chain-implied vol with no shape change.
    """

    available: bool
    days_to_earnings: int | None  # None if no upcoming earnings on file
    earnings_date: _date | None
    earnings_warning: bool  # True if within 5 trading days of earnings
    # False when the calendar has no upcoming report on file. A missing date
    # is a flag for the proposal engine, not a pass.
    earnings_known: bool = False
    # Suggested Black-Scholes strikes, one StrikeSet per expiry tenor
    # (empty when vol/price unavailable). Ordered nearest-expiry first.
    strike_sets: list[StrikeSet] = field(default_factory=list)
    # Curated observations: short bullets the user can read at a glance.
    observations: list[str] = field(default_factory=list)

    @classmethod
    def unavailable(cls) -> OptionsContext:
        return cls(
            available=False, days_to_earnings=None, earnings_date=None,
            earnings_warning=False, earnings_known=False,
            strike_sets=[], observations=[],
        )


@dataclass(frozen=True, slots=True)
class SymbolAnalysis:
    """Per-symbol full analysis bundle, the unit returned by the engine.

    Every nested state has its own ``available`` flag for soft-fail
    behavior. The top-level ``available`` indicates whether ANY part
    of the analysis ran successfully.
    """

    symbol: str
    as_of: _date  # the date the analysis was requested for
    available: bool
    last_close: float | None
    # Date of the last bar actually used. Lags ``as_of`` over weekends,
    # holidays and before the nightly refresh lands.
    last_bar_date: _date | None
    last_volume: float | None  # dollar volume on the most recent bar
    bars_count: int  # rows in the underlying frame (for diagnostics)
    trend: TrendState
    volatility: VolatilityState
    options_context: OptionsContext
    # Mean close × volume over the last 20 bars; None with fewer bars.
    adv_20d: float | None = None
    # 1-day % change of adj_close, and the same net of the sector composite's
    # 1-day return (None when the symbol has no sector composite).
    day_move_pct: float | None = None
    day_move_residual_pct: float | None = None
    # One day of vol in %: annualised (EWMA, else 21d) vol / √252.
    daily_sigma_pct: float | None = None
    # Keep a small slice of the raw close history so chart.py doesn't
    # need to re-query the DB. Indexed chronologically; most-recent
    # close is closes_history[-1]. Length capped at 252 trading days
    # (1 year) - enough context for chart visualization.
    closes_history: list[tuple[_date, float]] = field(default_factory=list)
    # Same for volumes (dollar volume) - used for chart sizing hints.
    volumes_history: list[tuple[_date, float]] = field(default_factory=list)
    # OHLCV candle records in Lightweight-Charts shape
    # ({time, open, high, low, close, volume}), chronological, capped at the
    # chart-history window. Powers the interactive candlestick mini-charts on
    # the /analysis hub (client-side time-window switching reuses this rather
    # than a second per-symbol bars fetch).
    ohlc_history: list[dict[str, float | str]] = field(default_factory=list)
    # Diagnostic - sub-modules that raised during compute.
    failures: list[str] = field(default_factory=list)

    @classmethod
    def unavailable(cls, symbol: str, as_of: _date, reason: str = "") -> SymbolAnalysis:
        return cls(
            symbol=symbol, as_of=as_of, available=False,
            last_close=None, last_bar_date=None, last_volume=None, bars_count=0,
            trend=TrendState.unavailable(),
            volatility=VolatilityState.unavailable(),
            options_context=OptionsContext.unavailable(),
            failures=[reason] if reason else [],
        )
