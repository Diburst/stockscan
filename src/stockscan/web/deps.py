"""FastAPI dependency factories.

Centralized so route handlers don't import db / templates ad-hoc.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import logging
import re
import secrets
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar

from fastapi import Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from sqlalchemy.orm import Session

from stockscan import __version__
from stockscan.db import session_scope

log = logging.getLogger(__name__)

T = TypeVar("T")


def safe(
    fn: Callable[[], T],
    *,
    default: T | None = None,
    label: str = "",
) -> T | None:
    """Call ``fn()``; on any exception, log a warning and return ``default``.

    Centralises the recurrent ``try → log.warning → fallback`` pattern that
    appears in route handlers loading optional / best-effort data
    (model artifacts, technical scores, regime context, etc.). Keeps the
    happy path readable while still surfacing failures in logs.

    Example::

        models = safe(list_models, default=[], label="strategies.list_models")
        artifact = safe(lambda: load_model(name), label=f"load_model[{name}]")
    """
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - that is the entire point
        log.warning("%s failed: %s", label or getattr(fn, "__name__", "safe"), exc)
        return default

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# Templates can call ``now_utc()`` to compute "x minutes ago" displays without
# the route needing to inject a timestamp into every render context. Returns
# a tz-aware UTC datetime so subtraction with stored timestamps is safe.
templates.env.globals["now_utc"] = lambda: datetime.now(UTC)


# ----------------------------------------------------------------------
# Tiny markdown-lite renderer for trusted in-app content (strategy manuals,
# tooltips, etc.). NOT for user-supplied content — escaping is minimal.
# Handles: ## / ### headings, **bold**, `inline code`, bullet lists, blank-line
# paragraph breaks. Anything else stays as preformatted text.
# ----------------------------------------------------------------------
_HEADING_RE = re.compile(r"^(#{2,4})\s+(.+)$", re.MULTILINE)
_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
_CODE_RE = re.compile(r"`([^`]+)`")


def _md_lite(text: str) -> Markup:
    if not text:
        return Markup("")
    # Escape first, then re-introduce safe spans.
    escaped = html.escape(text)

    # Headings: ##, ###, #### → h3, h4, h5.
    # `m-0` neutralizes browser-default margins; the surrounding
    # whitespace-pre-wrap container preserves the blank lines from the
    # source so spacing isn't doubled.
    def _heading(m: re.Match[str]) -> str:
        level = min(5, len(m.group(1)) + 1)
        body = m.group(2)
        cls = {
            3: "text-lg font-semibold m-0",
            4: "text-base font-semibold m-0",
            5: "font-semibold m-0 text-ink-700",
        }[level]
        return f'<h{level} class="{cls}">{body}</h{level}>'

    out = _HEADING_RE.sub(_heading, escaped)
    out = _BOLD_RE.sub(r"<strong>\1</strong>", out)
    out = _CODE_RE.sub(r'<code class="bg-ink-100 px-1 rounded text-xs">\1</code>', out)
    return Markup(out)


templates.env.filters["md_lite"] = _md_lite


# ----------------------------------------------------------------------
# Metadata humanizer — translates strategy / scan-output keys into a
# friendly label + short explanation tooltip on the Signal detail page.
# Curated rather than auto-generated so the prose stays accurate; new
# strategies should add entries here when they introduce new keys.
# ----------------------------------------------------------------------

# Mapping: metadata-key → (display_label, one-line explanation).
# The explanation appears as a hover tooltip and as collapsed body text.
_METADATA_LABELS: dict[str, tuple[str, str]] = {
    # ---- Donchian breakout ----
    "breakout_window": (
        "Breakout window",
        "Which entry window fired. 20 = sensitive (Turtle System 1, subject "
        "to the 1L skip-after-winner filter); 55 = confirmed (Turtle System "
        "2, always taken regardless of recent signal history).",
    ),
    "prior_max_close": (
        "Prior N-day max close",
        "Highest close in the N trading days BEFORE today, where N matches "
        "the breakout window. Today's close exceeded this level.",
    ),
    "adx": (
        "ADX(14)",
        "Average Directional Index — 0-100 scale measuring trend strength "
        "(not direction). Values above ~18 indicate a real trend; below means "
        "the market is choppy / range-bound.",
    ),
    "volume_mult_actual": (
        "Volume vs 20d avg",
        "Today's volume divided by its trailing 20-day mean. Donchian v1.1 "
        "requires this to be at least 1.5x to confirm institutional "
        "participation. Higher = stronger conviction.",
    ),
    "vol_expansion_ratio": (
        "True range / ATR(14)",
        "Today's true range divided by the 14-day ATR. >= 1.0 means today "
        "had at least average daily range — filters out wick-touch breakouts "
        "that closed at the high but had no real intraday movement.",
    ),
    "rs_60d_diff": (
        "60d return vs SPY",
        "Stock's 60-day return minus SPY's 60-day return. Positive = stock "
        "is outperforming the market over the trailing 60 days; the "
        "relative-strength filter requires this to be > 0.",
    ),
    "prior_signal_outcome": (
        "Prior 20d breakout outcome",
        "Outcome of the most recent prior 20-day breakout for this symbol "
        "under the v1.1 exit rules. 'winner' triggers the Turtle 1L filter "
        "(skip today's signal); 'loser' or 'none' allows it through.",
    ),
    # ---- Donchian v1.2: base-breakout filters ----
    "base_range_pct": (
        "Base width (20-bar)",
        "Range of the 20 bars BEFORE the breakout — (max high - min low) "
        "as % of midpoint. Donchian v1.2 requires <=12% to qualify as a "
        "tight consolidation base; wider 'bases' are usually just chop. "
        "Lower is tighter / cleaner.",
    ),
    "vol_contraction_ratio_actual": (
        "ATR(20) / ATR(63) pre-breakout",
        "Recent ATR(20) excluding today divided by longer-term ATR(63). "
        "Donchian v1.2 requires <=0.85 — i.e., recent vol must have "
        "compressed at least 15% relative to baseline (Bollinger Squeeze "
        "framing). Lower = stronger contraction = better base setup.",
    ),
    "pct_above_sma50": (
        "% above SMA(50)",
        "Today's close as a percent above the 50-day simple moving "
        "average. Donchian v1.2 caps this at 15% to filter stocks that "
        "are already extended off their long-term reference. Higher "
        "values mean a more mature run-up with worse risk:reward.",
    ),
    "rsi_pre_breakout": (
        "RSI(14) yesterday",
        "RSI(14) computed on YESTERDAY's close — the bar BEFORE today's "
        "breakout. Donchian v1.2 requires <65, since RSI >=65 going into "
        "the move means the stock was already overbought. Today's "
        "breakout bar will naturally pop RSI to 70+; that's expected.",
    ),
    # ---- Shared technical fields ----
    "atr": (
        "ATR(20)",
        "Average True Range over the last 20 days, in dollars. The typical "
        "daily price movement of this stock. Used to size the stop loss "
        "(stop = entry - 2 x ATR).",
    ),
    "atr_period": ("ATR period", "Lookback window for ATR (in trading days)."),
    "atr_stop_mult": (
        "ATR stop multiplier",
        "Multiplier applied to ATR to set the initial stop distance.",
    ),
    # ---- 52-week-high momentum ----
    "closeness_52w": (
        "Closeness to 52w high",
        "Today's close ÷ highest close in the last 252 days. 1.00 = fresh "
        "52-week high; 0.95 = within 5% of it. The strategy only emits "
        "signals at 0.95+.",
    ),
    "slope_quality": (
        "Slope quality",
        "Annualised log-return slope of the last 90 days, weighted by R² and "
        "sigmoid-normalised to [0, 1]. Higher = smoother uptrend. Used as a "
        "tiebreak between names at similar closeness.",
    ),
    "max_close_52w": (
        "52w high",
        "The highest close in the last 252 trading days (the denominator of "
        "the closeness ratio).",
    ),
    "holding_days": (
        "Planned holding period",
        "Trading days from entry to time-based exit. Matches the original "
        "George-Hwang study window.",
    ),
    # ---- RSI(2) mean reversion ----
    "rsi": (
        "RSI",
        "Relative Strength Index. 0-100 oscillator; below ~10 with RSI(2) "
        "indicates extreme oversold conditions where mean reversion is "
        "statistically likely.",
    ),
    "rsi_2": (
        "RSI(2)",
        "Two-period RSI — extremely sensitive to recent moves. The strategy "
        "buys when this drops below 10 (deep oversold) in an uptrend.",
    ),
    "rsi_period": ("RSI period", "Lookback window for the RSI calculation."),
    "sma200": (
        "SMA(200)",
        "200-day simple moving average — the long-term trend filter. The "
        "strategy only buys above SMA(200) to avoid catching falling knives.",
    ),
    # ---- LargeCap rebound (z-score-style) ----
    "z_score": (
        "Z-score",
        "Number of standard deviations today's close is below its 20-day "
        "mean. The strategy buys 2 std-dev+ pullbacks in trending uptrends.",
    ),
    "pullback_pct": (
        "Pullback %",
        "Percent drop from the 20-day high. Captures the depth of the dip "
        "the strategy is buying.",
    ),
    # Note: Z-score description uses 'std devs' rather than the unicode sigma
    # symbol so the file stays ruff RUF001-clean.
    # ---- Meta-labeling ----
    "meta_label_proba": (
        "Meta-label probability",
        "XGBoost-classifier estimate of P(this signal hits its profit-take "
        "barrier within the holding window). Score-only — never blocks "
        "trades. 0.50 = no opinion; 0.55+ = model has signal.",
    ),
}


def _humanize_metadata(metadata: object) -> list[dict[str, object]]:
    """Translate a JSONB metadata dict into a list of UI-renderable rows.

    Returns a list of ``{"key", "label", "value", "explanation",
    "is_known"}`` dicts in a deterministic order: known keys first
    (in their hand-curated order from :data:`_METADATA_LABELS`), then
    unknown keys alphabetically. Unknown keys still render — they just
    use the raw key as the label and have no explanation tooltip.
    """
    if not isinstance(metadata, dict):
        return []
    out: list[dict[str, object]] = []
    seen: set[str] = set()
    # Known keys first, in curated order.
    for key, (label, explanation) in _METADATA_LABELS.items():
        if key in metadata:
            out.append(
                {
                    "key": key,
                    "label": label,
                    "value": metadata[key],
                    "explanation": explanation,
                    "is_known": True,
                }
            )
            seen.add(key)
    # Unknown keys, alphabetically.
    for key in sorted(metadata):
        if key in seen:
            continue
        out.append(
            {
                "key": key,
                "label": key.replace("_", " ").title(),
                "value": metadata[key],
                "explanation": "",
                "is_known": False,
            }
        )
    return out


templates.env.filters["humanize_metadata"] = _humanize_metadata


# ----------------------------------------------------------------------
# Rejection-reason humanizer — translates the machine-readable codes
# stored in ``signals.rejected_reason`` into a friendly label + a
# longer tooltip explanation. Used by the rejected-signals card on
# /signals and the rejection banner on /signals/{id}.
#
# Codes come from two sources: the FilterChain in stockscan.risk.filters
# (most of them) and from strategies that emit a ``_strategy_reject_reason``
# in metadata (currently only Donchian's Turtle 1L). When a strategy
# adds a new reason it should add an entry here.
# ----------------------------------------------------------------------

# Static reasons (no dynamic substring). Mapping: code -> (label, explanation).
_REJECTION_REASONS_STATIC: dict[str, tuple[str, str]] = {
    "turtle_1l_skip_after_winner": (
        "Turtle 1L (skip after winner)",
        "Donchian's 20-day breakout was rejected because the previous 20-day "
        "breakout for this symbol would have been a winner. Per the original "
        "Turtle Traders rules, big sustained trends usually start AFTER a "
        "cluster of small false breakouts; once you've just had a winner, the "
        "next breakout has elevated false-positive risk. The 55-day window "
        "(System 2) acts as the failsafe and is always taken regardless.",
    ),
    "credit_stress_long_block": (
        "Credit stress (long block)",
        "HY OAS credit-stress flag was active and the signal is long. "
        "Credit-stress regimes historically lead equity drawdowns by 1-3 "
        "trading days, so new long entries are hard-blocked while the flag "
        "is on (per regime composite §Tier 0(b)).",
    ),
    "regime_zero_size": (
        "Regime sized to zero",
        "The composite regime multiplier (affinity x composite_mult x "
        "stress_mult) rounded the position size down to zero shares. "
        "Means the regime is hostile to this strategy at this time.",
    ),
    "qty_zero": (
        "Sized to zero",
        "The position sizer returned zero shares. Usually means the "
        "stop is too wide given equity and risk_pct, or risk_pct itself "
        "is misconfigured.",
    ),
    "earnings_within_5_trading_days": (
        "Earnings within 5 days",
        "The portfolio filter blocks new entries on names reporting "
        "earnings within the next 5 trading days. Avoids gap risk on "
        "the entry day; existing positions ride through.",
    ),
    "drawdown_circuit_breaker": (
        "Drawdown circuit breaker",
        "Total equity is more than the configured max drawdown below the "
        "high-water mark. All new entries blocked until equity recovers.",
    ),
    "filter_rejected": (
        "Filter rejected",
        "An unspecified filter rejected this signal. Check the runner "
        "logs for the specific filter name.",
    ),
}

# Dynamic-prefix reasons. Codes with variable substrings (e.g.,
# ``max_concurrent_positions_8``) match by ``startswith`` and the
# variable suffix is folded into the label.
_REJECTION_REASONS_DYNAMIC: list[tuple[str, str, str]] = [
    (
        "already_in_position_via_",
        "Already in a position",
        "Skipping because a position in this symbol already exists, "
        "opened by another strategy.",
    ),
    (
        "max_concurrent_positions_",
        "Max concurrent positions",
        "Portfolio-level cap on simultaneously open positions hit. "
        "Existing positions must close before new entries can open.",
    ),
    (
        "position_exceeds_",
        "Position-size cap (% of equity)",
        "Suggested notional exceeds the configured single-position cap "
        "as a fraction of total equity.",
    ),
    (
        "max_sector_pct_",
        "Sector concentration cap",
        "Adding this trade would push sector exposure above the "
        "portfolio-level cap.",
    ),
    (
        "max_adv_pct_",
        "ADV liquidity cap",
        "Suggested position exceeds the configured fraction of the "
        "symbol's 20-day average daily dollar volume. Liquidity check.",
    ),
]


def _humanize_rejection_reason(reason: object) -> dict[str, str]:
    """Translate a ``signals.rejected_reason`` code to UI-friendly form.

    Returns a dict ``{label, explanation, code, is_known}`` where
    ``label`` is short enough to display inline, ``explanation``
    is a longer hover tooltip, and ``is_known`` flags whether we
    had a curated entry (vs. fell through to title-casing).
    """
    if not isinstance(reason, str) or not reason:
        return {
            "label": "(rejected)",
            "explanation": "",
            "code": "",
            "is_known": False,
        }
    if reason in _REJECTION_REASONS_STATIC:
        label, explanation = _REJECTION_REASONS_STATIC[reason]
        return {
            "label": label,
            "explanation": explanation,
            "code": reason,
            "is_known": True,
        }
    for prefix, label, explanation in _REJECTION_REASONS_DYNAMIC:
        if reason.startswith(prefix):
            suffix = reason[len(prefix):]
            return {
                "label": f"{label} ({suffix})" if suffix else label,
                "explanation": explanation,
                "code": reason,
                "is_known": True,
            }
    # Unknown — fall back to title-casing so we still render something
    # readable rather than the raw snake_case code.
    return {
        "label": reason.replace("_", " ").title(),
        "explanation": "",
        "code": reason,
        "is_known": False,
    }


templates.env.filters["humanize_rejection_reason"] = _humanize_rejection_reason


# ----------------------------------------------------------------------
# Term explanations — `explain` Jinja filter. Maps a short key (e.g.,
# "rsi", "mfe", "sharpe") to a (display_label, hover_tooltip) pair.
# Templates render abbreviations through this filter to get a styled
# span with the tooltip applied:
#
#     {{ "rsi" | explain }}
#       → <span title="Relative Strength Index. ...">RSI</span>
#
# Falls back to the raw key (escaped, no tooltip) if not curated. Add
# new entries here when a new abbreviation appears in the UI.
# ----------------------------------------------------------------------

_TERM_EXPLANATIONS: dict[str, tuple[str, str]] = {
    # ---- Indicators ----
    "rsi": (
        "RSI",
        "Relative Strength Index — 0-100 momentum oscillator. <30 = oversold, "
        ">70 = overbought. Default lookback is 14.",
    ),
    "rsi_2": (
        "RSI(2)",
        "Two-period RSI — extremely sensitive to recent moves. Used by "
        "Larry Connors mean-reversion strategies; <10 = deep oversold.",
    ),
    "macd": (
        "MACD",
        "Moving Average Convergence/Divergence — difference between 12 and "
        "26-day EMAs, with a 9-day EMA signal line. Crosses signal trend changes.",
    ),
    "adx": (
        "ADX",
        "Average Directional Index — 0-100 trend-strength gauge (not "
        "direction). >18 = real trend; <18 = chop / range-bound.",
    ),
    "atr": (
        "ATR",
        "Average True Range — typical daily price movement (in dollars). "
        "Used to size stops (e.g., stop = entry - 2 x ATR).",
    ),
    "sma": (
        "SMA",
        "Simple Moving Average — arithmetic mean of the last N closes.",
    ),
    "sma200": (
        "SMA(200)",
        "200-day simple moving average — the standard long-term trend "
        "filter. Buy only above; sell only below.",
    ),
    "ema": (
        "EMA",
        "Exponential Moving Average — weighted MA that puts more weight on "
        "recent bars. Reacts faster than SMA.",
    ),
    "bb": (
        "Bollinger Bands",
        "Price envelope at SMA ± 2 standard deviations. %B measures where "
        "price sits within the bands (0 = lower, 1 = upper).",
    ),
    "pct_b": (
        "%B",
        "Bollinger %B — (price − lower band) / (upper band − lower band). "
        "0 = price at lower band; 1 = at upper; >1 = above the upper band.",
    ),
    "donchian": (
        "Donchian channel",
        "Upper/lower envelope of the highest high and lowest low over N "
        "days. Breakouts above the upper channel are classic trend entries.",
    ),
    "vwap": (
        "VWAP",
        "Volume-Weighted Average Price — average trade price weighted by "
        "volume across the session. Common intraday benchmark.",
    ),
    # ---- Trade / backtest metrics ----
    "r_multiple": (
        "R-multiple",
        "Trade outcome expressed as multiples of initial risk (1R = the "
        "dollars risked from entry to stop). +2R means the win was twice "
        "the risk taken.",
    ),
    "mfe": (
        "MFE",
        "Maximum Favorable Excursion — the best unrealised P&L the trade "
        "ever showed before exit. High MFE + small final P&L = took profit "
        "too late.",
    ),
    "mae": (
        "MAE",
        "Maximum Adverse Excursion — the worst unrealised drawdown the "
        "trade ever had before exit. Compares against the stop distance to "
        "see whether your stops are calibrated.",
    ),
    "sharpe": (
        "Sharpe",
        "Sharpe ratio — annualised excess return ÷ annualised return "
        "volatility. >1 is decent, >2 is strong; <0 means you'd have done "
        "better holding cash.",
    ),
    "sortino": (
        "Sortino",
        "Like Sharpe but only penalises downside volatility. Better gauge "
        "for asymmetric strategies (trend, mean-reversion) where upside "
        "vol is desirable.",
    ),
    "mar": (
        "MAR",
        "MAR ratio — CAGR ÷ max drawdown. A 'how much pain per unit of "
        "return' measure favoured by trend followers.",
    ),
    "cagr": (
        "CAGR",
        "Compound Annual Growth Rate — the constant annual rate that would "
        "produce the realised total return over the backtest window.",
    ),
    "max_dd": (
        "Max DD",
        "Maximum drawdown — largest peak-to-trough equity decline during "
        "the backtest, expressed as a percentage of the prior peak.",
    ),
    "drawdown": (
        "Drawdown",
        "Decline from the running equity peak to the current value, as a "
        "percentage. Recovery isn't counted until a new high is made.",
    ),
    "hit_rate": (
        "Hit rate",
        "Fraction of trades that closed profitable. Doesn't account for "
        "size of wins vs losses — use alongside expectancy.",
    ),
    "win_rate": (
        "Win rate",
        "Same as hit rate — fraction of trades that were profitable.",
    ),
    "profit_factor": (
        "Profit factor",
        "Total $ won ÷ total $ lost. >1 = profitable, >2 = strong, <1 = "
        "losing money.",
    ),
    "expectancy": (
        "Expectancy",
        "Average $ outcome per trade: (hit_rate × avg_win) − (loss_rate × "
        "avg_loss). Positive = the system has edge.",
    ),
    # ---- Position sizing ----
    "risk_pct": (
        "Risk %",
        "Fraction of total equity risked on a single trade (entry-to-stop "
        "distance × shares ÷ equity). Sizes the position so each loss is "
        "the same percentage hit.",
    ),
    "notional": (
        "Notional",
        "Dollar value of the position (price × shares). Used to enforce "
        "single-position-size and ADV liquidity caps.",
    ),
    "adv": (
        "ADV",
        "Average Daily Dollar Volume — the symbol's typical daily traded "
        "value. Position sizes are capped at a fraction of ADV so you can "
        "actually exit without moving the market.",
    ),
    # ---- Regime ----
    "regime": (
        "Regime",
        "Composite market state — a coarse classification of the current "
        "market (trending up, trending down, choppy, transitioning) that "
        "scales position sizes per strategy.",
    ),
    "trending_up": (
        "Trending up",
        "Composite regime: clear positive trend, strategies that buy "
        "strength get full size.",
    ),
    "trending_down": (
        "Trending down",
        "Composite regime: clear negative trend, long strategies are "
        "down-weighted or blocked.",
    ),
    "choppy": (
        "Choppy",
        "Composite regime: no directional edge. Trend strategies are sized "
        "down; mean-reversion may be sized up.",
    ),
    "transitioning": (
        "Transitioning",
        "Composite regime: the market is changing state. Conservative "
        "sizing applied across the board.",
    ),
    "affinity": (
        "Affinity",
        "Per-strategy alignment with the current regime — multiplier "
        "applied to base position size. 1.0 = no adjustment, <1 = trim, "
        ">1 = lean in.",
    ),
    "credit_stress": (
        "Credit stress",
        "HY OAS (high-yield option-adjusted spread) widening flag. Credit "
        "stress historically leads equity drawdowns by 1-3 trading days, "
        "so when it's on, new long entries are blocked.",
    ),
    # ---- Misc trade lifecycle ----
    "stop": (
        "Stop",
        "Hard stop-loss price. Position is exited if traded through.",
    ),
    "trailing_stop": (
        "Trailing stop",
        "Stop that ratchets up (long) as price moves favourably. Locks in "
        "gains on trending positions while keeping room for normal pullbacks.",
    ),
    "entry": (
        "Entry",
        "Price the strategy intends to enter at (typically the next open "
        "after the signal day).",
    ),
    "qty": (
        "Qty",
        "Suggested position size in shares, computed by the sizer from "
        "equity, risk %, and stop distance.",
    ),
    # ---- Meta-labeling ----
    "meta_label": (
        "Meta-label",
        "Lopez de Prado meta-labelling — a secondary classifier estimates "
        "P(this signal hits its profit target before its stop). Score-only; "
        "never blocks trades.",
    ),
}


def _explain(key: object) -> Markup:
    """Render an inline tooltip span for a known abbreviation key.

    Templates use this as a Jinja filter:

        {{ "rsi" | explain }}
          → <span title="Relative Strength Index. ..."
                  class="border-b border-dotted ...">RSI</span>

    Falls back to the raw key (escaped, no tooltip, no underline) for
    unknown terms so unknown values still render safely.
    """
    if not isinstance(key, str) or not key:
        return Markup("")
    entry = _TERM_EXPLANATIONS.get(key.lower().strip())
    if not entry:
        return Markup(html.escape(key))
    label, text = entry
    return Markup(
        f'<span title="{html.escape(text)}" '
        f'class="border-b border-dotted border-ink-300 cursor-help">'
        f"{html.escape(label)}</span>"
    )


def explain_text(key: object) -> str:
    """Plain-string variant of `_explain` — returns just the tooltip text.

    Useful when the calling site already has its own wrapper element and
    just needs the explanation string for a `title=` attribute.
    """
    if not isinstance(key, str) or not key:
        return ""
    entry = _TERM_EXPLANATIONS.get(key.lower().strip())
    return entry[1] if entry else ""


templates.env.filters["explain"] = _explain
templates.env.filters["explain_text"] = explain_text


# ----------------------------------------------------------------------
# Flash / toast plumbing.
#
# The UI shows transient toast notifications via two channels:
#
#   1. **Cookie flash** — for full-page POST→303→GET redirect flows. The
#      handler calls `flash_redirect(url, kind, message)` (or
#      `add_flash(response, ...)`) which sets a signed `_flash` cookie.
#      The next `render()` reads the cookie, clears it, and passes the
#      payload into the template context as `initial_toast`. base.html
#      renders it inside the toast region.
#
#   2. **HX-Trigger header** — for HTMX endpoints. The handler returns
#      `hx_toast_response(html, kind, message)` which attaches an
#      `HX-Trigger: {"toast": {...}}` header. base.html wires a JS
#      listener that appends a toast on the `toast` event.
#
# Cookies are HMAC-signed with a per-process random secret. They have a
# 30-second TTL; a process restart wipes pending toasts but they would
# have expired anyway. For a single-user local app this is sufficient
# and avoids the secret-management dependency.
# ----------------------------------------------------------------------

_FLASH_COOKIE_NAME = "_flash"
_FLASH_TTL_SECONDS = 30
_FLASH_SECRET = secrets.token_bytes(32)


def _sign(payload: str) -> str:
    sig = hmac.new(_FLASH_SECRET, payload.encode("utf-8"), hashlib.sha256).hexdigest()
    # First 16 hex chars (64 bits) is plenty for tamper-resistance on a
    # 30-second-TTL cookie that nobody but us is meant to read anyway.
    return sig[:16]


def _encode_flash(kind: str, message: str) -> str:
    payload = json.dumps({"kind": kind, "message": message}, separators=(",", ":"))
    return f"{_sign(payload)}.{payload}"


def _decode_flash(raw: str) -> dict[str, str] | None:
    if not raw or "." not in raw:
        return None
    sig, _, payload = raw.partition(".")
    if not sig or not payload:
        return None
    if not hmac.compare_digest(sig, _sign(payload)):
        return None
    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    kind = data.get("kind")
    message = data.get("message")
    if not isinstance(kind, str) or not isinstance(message, str):
        return None
    return {"kind": kind, "message": message}


def add_flash(response: Response, kind: str, message: str) -> Response:
    """Attach a flash toast to an existing response object.

    The toast will be displayed on the next page render that goes through
    `render()`. `kind` should be one of "success", "error", "warn", "info".
    """
    response.set_cookie(
        _FLASH_COOKIE_NAME,
        _encode_flash(kind, message),
        max_age=_FLASH_TTL_SECONDS,
        httponly=True,
        samesite="lax",
    )
    return response


def flash_redirect(
    url: str, kind: str, message: str, status_code: int = 303
) -> RedirectResponse:
    """Return a redirect response that also queues a flash toast.

    Convenience wrapper for the common POST → 303 redirect → GET pattern.
    Use this from any route that mutates state and wants to confirm the
    action with a toast on the next page.
    """
    response = RedirectResponse(url=url, status_code=status_code)
    return add_flash(response, kind, message)


def hx_toast_headers(kind: str, message: str) -> dict[str, str]:
    """Return HTMX response headers that fire a toast event.

    Use as `headers=hx_toast_headers(...)` when constructing an
    HTMLResponse from an HTMX endpoint. The `toast` event is dispatched
    on the body and picked up by the listener in base.html.
    """
    return {
        "HX-Trigger": json.dumps({"toast": {"kind": kind, "message": message}})
    }


def hx_toast_response(
    content: str | bytes, kind: str, message: str, status_code: int = 200
) -> HTMLResponse:
    """Return an HTML response (typically an HTMX swap) plus a toast trigger."""
    return HTMLResponse(
        content=content,
        status_code=status_code,
        headers=hx_toast_headers(kind, message),
    )


def attach_hx_toast(response: Response, kind: str, message: str) -> Response:
    """Attach an HX-Trigger toast event to an already-built response.

    Use this when a route already calls ``render()`` to produce its
    partial (e.g., the dashboard news/signals refresh handlers) and
    wants to additionally pop a toast on the client.
    """
    if not message:
        return response
    response.headers["HX-Trigger"] = json.dumps(
        {"toast": {"kind": kind, "message": message}}
    )
    return response


# ----------------------------------------------------------------------
# Rate limiter — single-process, single-user. Used by the refresh
# endpoints (POST /news/refresh, POST /signals/refresh) to debounce a
# user mashing the refresh button or browsers double-submitting.
#
# Per-key cooldown: rate_limit_check("news.refresh", 10) returns None
# if the action is allowed (and records the call), or the number of
# seconds the caller should wait. Caller decides what to render in the
# blocked case (typically: the current state + a "wait" toast).
# ----------------------------------------------------------------------

_RATE_LIMIT_LAST_CALL: dict[str, float] = {}


def rate_limit_check(key: str, cooldown_seconds: float) -> float | None:
    """Return None if allowed (records the call), else seconds remaining.

    Not thread-safe in the strict sense, but for a single-user local
    app the dict-write race is benign (worst case: two near-simultaneous
    refreshes both go through). No pruning; the dict has at most
    one entry per refresh endpoint, so it never grows.
    """
    now = time.monotonic()
    last = _RATE_LIMIT_LAST_CALL.get(key)
    if last is None or (now - last) >= cooldown_seconds:
        _RATE_LIMIT_LAST_CALL[key] = now
        return None
    return cooldown_seconds - (now - last)


def get_session() -> Iterator[Session]:
    """Yields a request-scoped DB session that auto-commits on success."""
    with session_scope() as s:
        yield s


def render(request: Request, template: str, **ctx) -> object:
    """Render a Jinja2 template with the standard request context attached.

    Reads and clears any pending flash cookie so a single toast survives
    one redirect. The toast payload is passed to the template as
    ``initial_toast`` (a dict with ``kind`` and ``message``) and rendered
    by ``toast_region()`` in ``base.html``.
    """
    initial_toast = None
    raw_flash = request.cookies.get(_FLASH_COOKIE_NAME)
    if raw_flash:
        initial_toast = _decode_flash(raw_flash)

    base_ctx = {
        "app_version": __version__,
        "initial_toast": initial_toast,
        **ctx,
    }
    response = templates.TemplateResponse(
        request=request, name=template, context=base_ctx
    )
    if raw_flash is not None:
        # Clear the cookie regardless of whether decoding succeeded —
        # tampered or expired cookies should not stick around.
        response.delete_cookie(_FLASH_COOKIE_NAME)
    return response
