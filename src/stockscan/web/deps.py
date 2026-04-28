"""FastAPI dependency factories.

Centralized so route handlers don't import db / templates ad-hoc.
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterator
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from sqlalchemy.orm import Session

from stockscan import __version__
from stockscan.db import session_scope

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


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
        cls = {3: "text-lg font-semibold m-0",
               4: "text-base font-semibold m-0",
               5: "font-semibold m-0 text-ink-700"}[level]
        return f'<h{level} class="{cls}">{body}</h{level}>'
    out = _HEADING_RE.sub(_heading, escaped)
    out = _BOLD_RE.sub(r'<strong>\1</strong>', out)
    out = _CODE_RE.sub(r'<code class="bg-ink-100 px-1 rounded text-xs">\1</code>', out)
    return Markup(out)


templates.env.filters["md_lite"] = _md_lite


def get_session() -> Iterator[Session]:
    """Yields a request-scoped DB session that auto-commits on success."""
    with session_scope() as s:
        yield s


def render(request: Request, template: str, **ctx) -> object:
    """Render a Jinja2 template with the standard request context attached."""
    base_ctx = {"app_version": __version__, **ctx}
    return templates.TemplateResponse(request=request, name=template, context=base_ctx)
