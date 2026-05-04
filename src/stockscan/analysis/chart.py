r"""SVG price chart renderer for the analysis pages.

Hand-rolled inline SVG. No external dependencies, no JavaScript,
renders fast on mobile, can be embedded directly into a Jinja
template via ``{{ chart_svg | safe }}``.

Layout:

    +------------------------------------------------------------+
    |     ┌── 90 days history ──┐  ┌── 30d projection ──┐         |
    |     │                     │  │ ░░ ±1sigma 30d band ░░ │         |
    |     │                  / \│  │     ┌─ ±1sigma 7d ─┐   │         |
    |     │   \_/\_  ___────\_/ │  │     │   band   │   │         |
    |     │ \/     \/            │  │     └──────────┘   │         |
    |     │ ─── SMA(20)          │  │   * current $X.XX  │         |
    |     │ ─── SMA(50)          │  └────────────────────┘         |
    |     │                     │                                  |
    |     ┝─R: $XX────────────  ┝────────────  resistance horizon  |
    |     ┝─S: $YY────────────  ┝────────────  support horizon     |
    +------------------------------------------------------------+

Components rendered:

  * **Price line** - last 90 days of close (from ``analysis.closes_history``).
  * **MA overlays** - SMA(20) and SMA(50) computed inside this module
    so the chart self-contains. Lighter strokes than the price line.
  * **Horizontal S/R lines** - every level in ``analysis.levels`` drawn
    as a horizontal band at the level's price, labeled.
  * **Forward range bands** - two shaded rectangles to the right of
    today's bar showing the 7d and 30d ±1sigma expected range.
  * **Current price marker** - labeled dot.

The output is XML (SVG markup as a string) which the template embeds
inline. We use only CSS-style SVG attributes so the chart renders
identically across browsers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from stockscan.analysis.state import SymbolAnalysis


# Default chart dimensions. The detail page can override; the listing
# uses these defaults.
_DEFAULT_WIDTH = 800
_DEFAULT_HEIGHT = 240
_PADDING_LEFT = 50  # room for y-axis labels
_PADDING_RIGHT = 110  # room for forward-projection bands + labels
_PADDING_TOP = 12
_PADDING_BOTTOM = 22  # room for x-axis date labels

# Colors - match the rest of the app's ink/ok/warn/bad palette.
_COLOR_PRICE = "#0f172a"  # ink-900
_COLOR_SMA20 = "#1d4ed8"  # blue-700, slightly muted
_COLOR_SMA50 = "#7c3aed"  # purple-600
_COLOR_GRID = "#e2e8f0"  # ink-200
_COLOR_AXIS = "#64748b"  # ink-500
_COLOR_SUPPORT = "#059669"  # ok-600
_COLOR_RESISTANCE = "#dc2626"  # bad-600
_COLOR_BAND_7D = "#fde68a"  # warm yellow, ~30% alpha applied via CSS
_COLOR_BAND_30D = "#fed7aa"  # warm orange-yellow
_COLOR_CURRENT = "#0f172a"


def render_chart_svg(
    analysis: SymbolAnalysis,
    *,
    width: int = _DEFAULT_WIDTH,
    height: int = _DEFAULT_HEIGHT,
    history_days: int = 90,
) -> str:
    """Render an inline SVG chart for one ``SymbolAnalysis``.

    Returns SVG markup as a string. The template embeds this with
    ``{{ chart_svg | safe }}``. If the analysis is unavailable or
    has insufficient history, returns a minimal "no data" SVG so
    the layout slot still renders.
    """
    if not analysis.available or not analysis.closes_history:
        return _placeholder_svg(width, height, "No bars history")

    # Slice to the requested history window.
    history = analysis.closes_history[-history_days:]
    if len(history) < 5:
        return _placeholder_svg(width, height, "Insufficient history")

    closes = [c for _, c in history]
    last_close = analysis.last_close or closes[-1]

    # Compute MA overlays inline so the chart module self-contains.
    sma20 = _rolling_mean(closes, 20)
    sma50 = _rolling_mean(closes, 50)

    # Y-axis range. Include current closes + S/R levels + forward
    # range bands so everything fits.
    y_values: list[float] = list(closes)
    for lv in analysis.levels:
        y_values.append(lv.price)
    if analysis.volatility.expected_30d:
        y_values.append(analysis.volatility.expected_30d.high)
        y_values.append(analysis.volatility.expected_30d.low)
    y_min = min(y_values) * 0.985
    y_max = max(y_values) * 1.015
    y_span = y_max - y_min

    # X-axis: history bars use the LEFT region; forward projection
    # uses a slice of the RIGHT padding.
    plot_x_left = _PADDING_LEFT
    # Reserve ~30% of the right pad for the forward band visualization.
    forward_pad = 80
    plot_x_right = width - _PADDING_RIGHT
    plot_y_top = _PADDING_TOP
    plot_y_bottom = height - _PADDING_BOTTOM

    bar_count = len(history)
    if bar_count <= 1:
        return _placeholder_svg(width, height, "Insufficient history")

    def x_for(i: int) -> float:
        # i=0 → plot_x_left; i=bar_count-1 → plot_x_right
        return plot_x_left + (plot_x_right - plot_x_left) * i / (bar_count - 1)

    def y_for(price: float) -> float:
        # Inverted: high prices map to low y (top of chart).
        return plot_y_top + (plot_y_bottom - plot_y_top) * (1 - (price - y_min) / y_span)

    # ---- Build SVG fragments ----
    parts: list[str] = []
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 {width} {height}" '
        f'width="100%" height="auto" '
        f'style="font-family: ui-sans-serif, system-ui, sans-serif; '
        f'font-size: 10px; color: {_COLOR_AXIS};">'
    )

    # Background grid - horizontal lines at 4 evenly-spaced y values.
    parts.append('<g stroke="' + _COLOR_GRID + '" stroke-width="0.5">')
    for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
        y = plot_y_top + (plot_y_bottom - plot_y_top) * frac
        parts.append(
            f'<line x1="{plot_x_left}" y1="{y:.1f}" x2="{plot_x_right + forward_pad}" y2="{y:.1f}" />'
        )
    parts.append('</g>')

    # Y-axis labels - show the min, mid, and max prices.
    for frac, price in (
        (0.0, y_max),
        (0.5, y_min + y_span * 0.5),
        (1.0, y_min),
    ):
        y = plot_y_top + (plot_y_bottom - plot_y_top) * frac
        parts.append(
            f'<text x="{plot_x_left - 4}" y="{y + 3:.1f}" '
            f'text-anchor="end" fill="{_COLOR_AXIS}">${price:.2f}</text>'
        )

    # X-axis labels - leftmost (oldest), midpoint, rightmost (today).
    if history:
        parts.append(
            f'<text x="{plot_x_left}" y="{height - 4}" text-anchor="start" '
            f'fill="{_COLOR_AXIS}">{history[0][0]}</text>'
        )
        mid_idx = len(history) // 2
        parts.append(
            f'<text x="{(plot_x_left + plot_x_right) / 2:.0f}" y="{height - 4}" '
            f'text-anchor="middle" fill="{_COLOR_AXIS}">{history[mid_idx][0]}</text>'
        )
        parts.append(
            f'<text x="{plot_x_right}" y="{height - 4}" text-anchor="end" '
            f'fill="{_COLOR_AXIS}">{history[-1][0]}</text>'
        )

    # Forward-projection bands (drawn FIRST so they sit under the lines).
    # 30-day band is wider; 7-day is narrower nested inside it.
    if analysis.volatility.expected_30d is not None:
        er = analysis.volatility.expected_30d
        band_x = plot_x_right
        band_w = forward_pad
        y_high = y_for(er.high)
        y_low = y_for(er.low)
        parts.append(
            f'<rect x="{band_x:.1f}" y="{y_high:.1f}" '
            f'width="{band_w}" height="{(y_low - y_high):.1f}" '
            f'fill="{_COLOR_BAND_30D}" fill-opacity="0.40" '
            f'stroke="{_COLOR_BAND_30D}" stroke-width="0.5" />'
        )
        # Label the band on the right side
        parts.append(
            f'<text x="{band_x + band_w + 2}" y="{y_high - 2:.1f}" '
            f'fill="{_COLOR_AXIS}">30d ±1sigma</text>'
        )
        parts.append(
            f'<text x="{band_x + band_w + 2}" y="{y_low + 8:.1f}" '
            f'fill="{_COLOR_AXIS}">${er.low:.0f}-${er.high:.0f}</text>'
        )
    if analysis.volatility.expected_7d is not None:
        er = analysis.volatility.expected_7d
        # 7-day band: narrower x-range, sits inside 30d band
        band_x = plot_x_right
        band_w = forward_pad * 0.45  # narrower
        y_high = y_for(er.high)
        y_low = y_for(er.low)
        parts.append(
            f'<rect x="{band_x:.1f}" y="{y_high:.1f}" '
            f'width="{band_w:.1f}" height="{(y_low - y_high):.1f}" '
            f'fill="{_COLOR_BAND_7D}" fill-opacity="0.55" '
            f'stroke="{_COLOR_BAND_7D}" stroke-width="0.5" />'
        )

    # Horizontal S/R lines.
    for lv in analysis.levels:
        y = y_for(lv.price)
        color = _COLOR_SUPPORT if lv.kind == "support" else _COLOR_RESISTANCE
        parts.append(
            f'<line x1="{plot_x_left}" y1="{y:.1f}" '
            f'x2="{plot_x_right}" y2="{y:.1f}" '
            f'stroke="{color}" stroke-width="1" stroke-dasharray="4 3" '
            f'opacity="{0.30 + 0.70 * lv.strength:.2f}" />'
        )
        # Tag at left edge: "R: $X" or "S: $X"
        kind_letter = "R" if lv.kind == "resistance" else "S"
        parts.append(
            f'<text x="{plot_x_left + 2}" y="{y - 2:.1f}" '
            f'fill="{color}" font-size="9" font-weight="600">'
            f'{kind_letter} ${lv.price:.2f}</text>'
        )

    # MA overlays (SMA20 then SMA50).
    sma20_path = _polyline_path(
        [(x_for(i), y_for(v)) for i, v in enumerate(sma20) if v is not None]
    )
    if sma20_path:
        parts.append(
            f'<path d="{sma20_path}" stroke="{_COLOR_SMA20}" '
            f'stroke-width="1" fill="none" opacity="0.60" />'
        )
    sma50_path = _polyline_path(
        [(x_for(i), y_for(v)) for i, v in enumerate(sma50) if v is not None]
    )
    if sma50_path:
        parts.append(
            f'<path d="{sma50_path}" stroke="{_COLOR_SMA50}" '
            f'stroke-width="1" fill="none" opacity="0.50" />'
        )

    # Price line - drawn last so it sits ON TOP of MAs and bands.
    price_path = _polyline_path(
        [(x_for(i), y_for(c)) for i, c in enumerate(closes)]
    )
    parts.append(
        f'<path d="{price_path}" stroke="{_COLOR_PRICE}" '
        f'stroke-width="1.6" fill="none" />'
    )

    # Current price marker + label.
    cx = x_for(bar_count - 1)
    cy = y_for(last_close)
    parts.append(
        f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="3" '
        f'fill="{_COLOR_CURRENT}" />'
    )
    # Place label to the LEFT of the marker if the forward band exists,
    # so it doesn't collide. Otherwise to the right.
    label_x = cx + 6
    parts.append(
        f'<text x="{label_x:.1f}" y="{cy + 4:.1f}" '
        f'fill="{_COLOR_CURRENT}" font-weight="600">'
        f'${last_close:.2f}</text>'
    )

    # Mini legend in the top-left.
    parts.append(
        f'<g transform="translate({plot_x_left + 4}, {plot_y_top + 8})" '
        f'font-size="9">'
    )
    parts.append(
        f'<rect x="0" y="-8" width="14" height="2" fill="{_COLOR_PRICE}" />'
        f'<text x="18" y="-2" fill="{_COLOR_AXIS}">close</text>'
    )
    parts.append(
        f'<rect x="55" y="-8" width="14" height="2" fill="{_COLOR_SMA20}" />'
        f'<text x="73" y="-2" fill="{_COLOR_AXIS}">SMA20</text>'
    )
    parts.append(
        f'<rect x="115" y="-8" width="14" height="2" fill="{_COLOR_SMA50}" />'
        f'<text x="133" y="-2" fill="{_COLOR_AXIS}">SMA50</text>'
    )
    parts.append('</g>')

    parts.append('</svg>')
    return "".join(parts)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _rolling_mean(values: list[float], window: int) -> list[float | None]:
    """Simple rolling mean - returns a list of len(values) with None
    for indices that don't have a full window of history yet."""
    out: list[float | None] = [None] * len(values)
    if len(values) < window:
        return out
    s = sum(values[:window])
    out[window - 1] = s / window
    for i in range(window, len(values)):
        s += values[i] - values[i - window]
        out[i] = s / window
    return out


def _polyline_path(points: list[tuple[float, float]]) -> str:
    """Build an SVG path 'M x y L x y L x y ...' from a list of points.

    Skips any None values; produces multiple sub-paths separated by
    'M' commands when there are gaps. Returns "" for empty input.
    """
    if not points:
        return ""
    cmds: list[str] = []
    cmds.append(f"M {points[0][0]:.1f} {points[0][1]:.1f}")
    for x, y in points[1:]:
        cmds.append(f"L {x:.1f} {y:.1f}")
    return " ".join(cmds)


def _placeholder_svg(width: int, height: int, message: str) -> str:
    """Render a minimal 'no data' SVG so the layout slot still has content."""
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 {width} {height}" width="100%" height="auto" '
        f'style="font-family: ui-sans-serif, system-ui, sans-serif;">'
        f'<rect x="0" y="0" width="{width}" height="{height}" '
        f'fill="#f1f5f9" stroke="#cbd5e1" stroke-width="1" stroke-dasharray="4 4" />'
        f'<text x="{width / 2}" y="{height / 2 + 4}" '
        f'text-anchor="middle" fill="#64748b" font-size="14">{message}</text>'
        f'</svg>'
    )
