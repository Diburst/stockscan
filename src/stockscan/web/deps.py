"""FastAPI dependency factories.

Centralized so route handlers don't import db / templates ad-hoc.
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from sqlalchemy.orm import Session

from stockscan import __version__
from stockscan.db import session_scope

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


def get_session() -> Iterator[Session]:
    """Yields a request-scoped DB session that auto-commits on success."""
    with session_scope() as s:
        yield s


def render(request: Request, template: str, **ctx) -> object:
    """Render a Jinja2 template with the standard request context attached."""
    base_ctx = {"app_version": __version__, **ctx}
    return templates.TemplateResponse(request=request, name=template, context=base_ctx)
