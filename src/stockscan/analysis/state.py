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
class Level:
    """One support or resistance level with strength scoring.

    Levels are price values where the symbol has historically reversed.
    The ``strength`` field is a [0, 1] composite of touch count and
    recency - higher means more historically significant.

    **Polarity / role-reversal.** ``kind`` is set from the level's
    relationship to the *current* close, not from the pivot type that
    originally produced it: anything below current price is "support",
    anything above is "resistance". The pivot origin is preserved
    separately in ``origin``. When ``kind`` and ``origin`` disagree,
    the level has flipped roles - a broken resistance now acting as
    support, or a broken support now acting as resistance. Use the
    ``is_flipped`` property to detect that case in the UI; flipped
    levels are notable setups in classical TA and worth surfacing
    distinctly from never-tested levels.
    """

    price: float
    kind: str  # 'support' | 'resistance' — determined by price vs last_close
    strength: float  # [0, 1]
    touches: int  # how many times price reversed near this level
    last_touch_days_ago: int  # 0 = today, larger = older
    distance_pct: float  # signed % from current close (negative = below)
    origin: str = "pivot_high"  # 'pivot_high' | 'pivot_low' — pivot type that produced this level

    @property
    def is_flipped(self) -> bool:
        """True when role differs from pivot origin (broken-and-reversed level).

        ``pivot_high`` origin + ``support`` kind = an old resistance the
        market broke through and is now defending as floor.
        ``pivot_low`` origin + ``resistance`` kind = an old support the
        market broke through and is now hitting as ceiling.
        """
        return (
            (self.origin == "pivot_high" and self.kind == "support")
            or (self.origin == "pivot_low" and self.kind == "resistance")
        )


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
    adx_14: float | None
    # Distance of close from each MA, in % (signed; positive = above MA)
    pct_above_sma20: float | None
    pct_above_sma50: float | None
    pct_above_sma200: float | None

    @classmethod
    def unavailable(cls) -> TrendState:
        return cls(
            available=False, bucket="?", label="n/a",
            explanation="Insufficient bars to assess trend.",
            return_5d=None, return_21d=None, return_63d=None,
            ma_alignment="mixed",
            sma_20=None, sma_50=None, sma_200=None, adx_14=None,
            pct_above_sma20=None, pct_above_sma50=None,
            pct_above_sma200=None,
        )


@dataclass(frozen=True, slots=True)
class VolatilityState:
    available: bool
    realized_vol_21d_pct: float | None  # annualised, in %
    realized_vol_63d_pct: float | None
    atr_14: float | None  # in dollars
    atr_pct_of_price: float | None  # ATR as % of current price
    bb_width_pct: float | None  # (upper - lower) / middle, as %
    hv_percentile: float | None  # 0-100; current 21d realized vol's rank in 252-day distribution
    expected_7d: ExpectedRange | None
    expected_30d: ExpectedRange | None
    bucket: str  # 'low' | 'normal' | 'elevated' | 'high' | '?'
    label: str
    explanation: str

    @classmethod
    def unavailable(cls) -> VolatilityState:
        return cls(
            available=False, realized_vol_21d_pct=None,
            realized_vol_63d_pct=None, atr_14=None,
            atr_pct_of_price=None, bb_width_pct=None,
            hv_percentile=None, expected_7d=None, expected_30d=None,
            bucket="?", label="n/a",
            explanation="Insufficient bars to compute volatility metrics.",
        )


@dataclass(frozen=True, slots=True)
class MomentumState:
    available: bool
    rsi_14: float | None
    rsi_bucket: str  # 'oversold' | 'low' | 'neutral' | 'high' | 'overbought' | '?'
    rsi_label: str
    macd_line: float | None
    macd_signal: float | None
    macd_histogram: float | None
    macd_state: str  # 'bullish_cross' | 'bullish' | 'neutral' | 'bearish' | 'bearish_cross' | '?'
    macd_label: str
    explanation: str

    @classmethod
    def unavailable(cls) -> MomentumState:
        return cls(
            available=False, rsi_14=None, rsi_bucket="?",
            rsi_label="n/a", macd_line=None, macd_signal=None,
            macd_histogram=None, macd_state="?", macd_label="n/a",
            explanation="Insufficient bars to compute momentum.",
        )


@dataclass(frozen=True, slots=True)
class OptionsContext:
    """Options-trading-flavored framing of the technicals.

    NOT a substitute for actual option chain data - this is the
    closest we can get with daily bars + earnings calendar alone.
    When option chain integration ships, the IV percentile + implied
    move will replace the realized-vol approximations here.
    """

    available: bool
    days_to_earnings: int | None  # None if no upcoming earnings on file
    earnings_date: _date | None
    earnings_warning: bool  # True if within 5 trading days of earnings
    # Position framing
    nearest_support: Level | None
    nearest_resistance: Level | None
    pct_to_support: float | None
    pct_to_resistance: float | None
    # Curated observations: short bullets the user can read at a glance.
    observations: list[str] = field(default_factory=list)

    @classmethod
    def unavailable(cls) -> OptionsContext:
        return cls(
            available=False, days_to_earnings=None, earnings_date=None,
            earnings_warning=False, nearest_support=None,
            nearest_resistance=None, pct_to_support=None,
            pct_to_resistance=None, observations=[],
        )


@dataclass(frozen=True, slots=True)
class SymbolAnalysis:
    """Per-symbol full analysis bundle, the unit returned by the engine.

    Every nested state has its own ``available`` flag for soft-fail
    behavior. The top-level ``available`` indicates whether ANY part
    of the analysis ran successfully.
    """

    symbol: str
    as_of: _date
    available: bool
    last_close: float | None
    last_volume: float | None  # dollar volume on the most recent bar
    bars_count: int  # rows in the underlying frame (for diagnostics)
    levels: list[Level]
    trend: TrendState
    volatility: VolatilityState
    momentum: MomentumState
    options_context: OptionsContext
    # Keep a small slice of the raw close history so chart.py doesn't
    # need to re-query the DB. Indexed chronologically; most-recent
    # close is closes_history[-1]. Length capped at 252 trading days
    # (1 year) - enough context for chart visualization.
    closes_history: list[tuple[_date, float]] = field(default_factory=list)
    # Same for volumes (dollar volume) - used for chart sizing hints.
    volumes_history: list[tuple[_date, float]] = field(default_factory=list)
    # Diagnostic - sub-modules that raised during compute.
    failures: list[str] = field(default_factory=list)

    @classmethod
    def unavailable(cls, symbol: str, as_of: _date, reason: str = "") -> SymbolAnalysis:
        return cls(
            symbol=symbol, as_of=as_of, available=False,
            last_close=None, last_volume=None, bars_count=0,
            levels=[],
            trend=TrendState.unavailable(),
            volatility=VolatilityState.unavailable(),
            momentum=MomentumState.unavailable(),
            options_context=OptionsContext.unavailable(),
            failures=[reason] if reason else [],
        )
