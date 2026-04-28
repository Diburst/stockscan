"""Technical indicators used by strategies and analysis.

Hand-rolled, dependency-light, NaN-safe. No `pandas-ta` — that library is
unmaintained and brittle on newer numpy/pandas. The set below covers
everything our v1 strategies need; add more here as new strategies require.

All functions accept a price/bar Series or DataFrame and return a Series
aligned to the input index. NaN at the start (insufficient history) is
intentional — never silently filled.
"""

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

__all__ = [
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
]
