"""Technical indicators used by strategies and analysis.

Hand-rolled, dependency-light, NaN-safe. No `pandas-ta` — that library is
unmaintained and brittle on newer numpy/pandas. The set below covers
everything our v1 strategies need; add more here as new strategies require.

All functions accept a price/bar Series or DataFrame and return a Series
aligned to the input index. NaN at the start (insufficient history) is
intentional — never silently filled.
"""

from stockscan.indicators.fibonacci import fibonacci_retracement
from stockscan.indicators.pivots import pivot_proximity
from stockscan.indicators.relative_strength import (
    relative_strength_values,
    sector_relative_strength,
)
from stockscan.indicators.reversal import reversal_trigger
from stockscan.indicators.ta import (
    adx,
    atr,
    avg_dollar_volume,
    bollinger_bands,
    donchian_channel,
    ema,
    macd,
    rsi,
    sma,
    true_range,
)
from stockscan.indicators.trend import trend_location
from stockscan.indicators.volume import volume_confirm

__all__ = [
    # --- primitives: moving averages / oscillators / ranges (ta.py) ---
    "adx",
    "atr",
    "avg_dollar_volume",
    "bollinger_bands",
    "donchian_channel",
    "ema",
    "macd",
    "rsi",
    "sma",
    "true_range",
    # --- primitives: chart studies ---
    "fibonacci_retracement",
    # --- primitives: reversal-composite building blocks ---
    "pivot_proximity",
    "relative_strength_values",
    "reversal_trigger",
    "sector_relative_strength",
    "trend_location",
    "volume_confirm",
]
