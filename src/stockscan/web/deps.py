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
    (regime context, data-freshness lookups, etc.). Keeps the happy path
    readable while still surfacing failures in logs.

    Example::

        regime = safe(lambda: get_regime(as_of, session=s), label="regime")
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


def _data_features() -> frozenset[str]:
    """Enabled EODHD endpoint families (EODHD_FEATURES) for templates.

    Templates test ``'news' in data_features()`` to swap a refresh button
    for a muted "not available on current data plan" note. Read live (not
    cached) so a settings override in tests is honoured.
    """
    from stockscan.config import settings

    return settings.eodhd_feature_set


templates.env.globals["data_features"] = _data_features
templates.env.globals["DATA_PLAN_NOTE"] = "not available on current data plan"


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
    # ---- RSI(2) pullback ----
    "rsi_2": (
        "RSI(2)",
        "Two-period RSI on adjusted close — reads 'the stock fell hard over "
        "the last two sessions'. The strategy buys below 10.",
    ),
    "sma_200": (
        "SMA(200)",
        "200-day simple moving average of adjusted close — the long-term "
        "trend filter. Both strategies require the close above it.",
    ),
    "stock_return_1m": (
        "Stock 1-month return",
        "The stock's own return over the last 21 trading days. Compared "
        "against its sector composite to tell a stock-specific dip from a "
        "sector-wide one.",
    ),
    "sector_return_1m": (
        "Sector 1-month return",
        "The stock's equal-weight sector composite's return over the last 21 "
        "trading days. Below -5% the strategy stands aside — a sector in a "
        "downtrend keeps falling.",
    ),
    "idiosyncratic_drop": (
        "Idiosyncratic drop",
        "Sector 1-month return minus stock 1-month return: how much more the "
        "stock fell than its sector. This is the ranking score — the most "
        "stock-specific drop goes first.",
    ),
    "relative_volume": (
        "Selloff volume vs normal",
        "Mean volume over the two selloff bars divided by the 50-day baseline "
        "before them. At 1.5x or more the setup is skipped — heavy-volume "
        "declines tend to continue.",
    ),
    # ---- 52-week-high momentum ----
    "closeness_52w": (
        "Closeness to 52w high",
        "Today's close divided by the highest close in the last 252 days. 1.00 "
        "= fresh 52-week high; 0.90 = within 10% of it, the entry gate.",
    ),
    "slope_quality": (
        "Slope quality",
        "Clenow's ranking metric: 90-day regression slope of log price, "
        "annualized and weighted by R-squared, squashed to (0, 1). Steep and "
        "smooth beats steep and jagged.",
    ),
    "residual_return_12m": (
        "Residual 12-month return",
        "The stock's 252-day return minus its sector composite's — the part "
        "of the climb that is the stock's own rather than a sector wave.",
    ),
    "residual_tilt": (
        "Residual tilt",
        "The residual 12-month return capped to +/-25%, added to the rank so "
        "idiosyncratic momentum (the part that does not crash) leads.",
    ),
    "realized_vol_1y": (
        "Realized vol (1y)",
        "Annualized standard deviation of daily returns over the last year. "
        "Above 60% the name is skipped — momentum crashes concentrate in the "
        "highest-beta names.",
    ),
    "sma_50": (
        "SMA(50)",
        "50-day simple moving average of adjusted close. Must sit above the "
        "200-day for the uptrend to count as confirmed.",
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
# Codes come from the regime layer in the scan runner (the trend gate and
# the credit-stress breaker) and from the FilterChain in
# stockscan.risk.filters. When a filter adds a new reason it should add an
# entry here.
# ----------------------------------------------------------------------

# Static reasons (no dynamic substring). Mapping: code -> (label, explanation).
_REJECTION_REASONS_STATIC: dict[str, tuple[str, str]] = {
    "trend_gate_closed": (
        "Trend gate closed",
        "SPY had closed below its 200-day SMA for three or more consecutive "
        "sessions, so the regime layer refused the new long entry. Open "
        "positions keep running their own exits; the gate governs entries "
        "only, and reopens after three closes back above the line.",
    ),
    "credit_stress_long_block": (
        "Credit stress (long block)",
        "HY OAS was in the top 15% of its trailing year and still rising, "
        "and the signal is long. Credit stress historically leads equity "
        "drawdowns by 1-3 trading days, so new long entries are refused "
        "while the breaker fires.",
    ),
    "qty_zero": (
        "Sized to zero",
        "The position sizer returned zero shares. Usually means the "
        "stop is too wide given equity and the strategy's risk fraction, "
        "or the vol scalar shrank a small allocation below one share.",
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
    "position_pct": (
        "Position %",
        "Fixed fraction of equity per position, used by strategies that "
        "emit no stop (there is no risk-per-share to size from).",
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
        "Market-health label derived from two flags: risk_on (trend gate "
        "open), risk_off (gate closed — no new longs) or credit_stress "
        "(HY OAS breaker firing — no new longs).",
    ),
    "risk_on": (
        "Risk on",
        "SPY has held above its 200-day SMA for at least three closes and "
        "credit is calm. New entries allowed at the strategy's own size, "
        "scaled by the vol scalar where it applies.",
    ),
    "risk_off": (
        "Risk off",
        "SPY has closed below its 200-day SMA for three or more sessions. "
        "New long entries are refused; open positions run their own exits.",
    ),
    "trend_gate": (
        "Trend gate",
        "SPY vs its 200-day SMA with a three-close dwell so a single cross "
        "does not flip it. Closed = no new long entries. Evidence is "
        "drawdown reduction, not return prediction.",
    ),
    "vol_scalar": (
        "Vol scalar",
        "Position-size multiplier in [0.5, 1]. When 20-day realized SPY vol "
        "is in the top tercile of its trailing year the size shrinks toward "
        "16% / realized; otherwise 1.0. Applies only to strategies that opt "
        "in (momentum, not mean reversion).",
    ),
    "credit_stress": (
        "Credit stress",
        "HY OAS (high-yield option-adjusted spread) in the top 15% of its "
        "trailing year and rising. Credit stress historically leads equity "
        "drawdowns by 1-3 trading days, so while it fires new long entries "
        "are refused.",
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
        "Suggested position size in shares — risk fraction over the stop "
        "distance for stop-based strategies, a fixed fraction of equity "
        "for stop-less ones, then the vol scalar where it applies.",
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
    partial (e.g., the backtest run handlers) and
    wants to additionally pop a toast on the client.
    """
    if not message:
        return response
    response.headers["HX-Trigger"] = json.dumps(
        {"toast": {"kind": kind, "message": message}}
    )
    return response


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
