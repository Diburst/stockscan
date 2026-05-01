"""Documentation hub — renders the project's markdown docs + CLI reference.

Three pages:

  * GET /docs         — index (linked cards for each known doc + CLI)
  * GET /docs/cli     — auto-generated CLI reference, walks the Typer tree
                        and captures `--help` for every command/group/leaf
  * GET /docs/{slug}  — renders one of the registered markdown files via
                        the Python ``markdown`` library, with auto-generated
                        TOC + heading anchors.

The Swagger UI (which used to live at /docs) is still available at
``/api-docs`` — see ``web/app.py`` for the relocation.

Markdown source files live at the repository root. Adding a new doc
just means appending an entry to :data:`_DOCS` — no template changes
required.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import click
import markdown as markdown_lib
import typer
from fastapi import APIRouter, HTTPException, Request
from markupsafe import Markup

from stockscan.web.deps import render

router = APIRouter(prefix="/docs")
log = logging.getLogger(__name__)


# Repository root, resolved from this file's location: the source tree
# is src/stockscan/web/routes/manual.py → ../../../../ is the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[4]


@dataclass(frozen=True, slots=True)
class _DocEntry:
    """One registered documentation file.

    Slugs are kebab-case and form the URL: /docs/<slug>. Titles
    appear as the H1 on the rendered page and as the card label on
    the index. Subtitles describe the doc in one sentence for the
    index card.
    """

    slug: str
    title: str
    subtitle: str
    filename: str  # path relative to repo root

    @property
    def path(self) -> Path:
        return _REPO_ROOT / self.filename


# Registered markdown docs, in the order they appear on the index.
# Add a new file by appending an entry here; no template changes
# required. The slug is what appears in the URL (/docs/<slug>).
_DOCS: tuple[_DocEntry, ...] = (
    _DocEntry(
        slug="readme",
        title="Getting Started",
        subtitle=(
            "Setup, day-to-day commands, web UI overview. The first "
            "doc to read on a new machine."
        ),
        filename="README.md",
    ),
    _DocEntry(
        slug="design",
        title="System Design",
        subtitle=(
            "Authoritative design doc: architecture, module breakdown, "
            "strategy specifications, schema, deployment, roadmap."
        ),
        filename="DESIGN.md",
    ),
    _DocEntry(
        slug="user-stories",
        title="User Stories",
        subtitle=(
            "Functional spec — the workflows the app needs to support, "
            "with concrete UI mockups and acceptance criteria."
        ),
        filename="USER_STORIES.md",
    ),
    _DocEntry(
        slug="todo",
        title="Roadmap & TODO",
        subtitle=(
            "Backlog of deferred features, ordered by impact, with "
            "enough context to pick any item up cold."
        ),
        filename="TODO.md",
    ),
    _DocEntry(
        slug="migration",
        title="Migrations",
        subtitle=(
            "How the custom SQL migration runner works, the rules for "
            "writing new migrations, and what the runner won't do."
        ),
        filename="MIGRATION.md",
    ),
    _DocEntry(
        slug="regime-research",
        title="Regime Detector — Research",
        subtitle=(
            "Background research that shaped the v2 composite regime "
            "classifier (vol/trend/breadth/credit weights, HY OAS lead, "
            "no-look-ahead invariants)."
        ),
        filename="market_regime_detection.md",
    ),
)


def _docs_by_slug() -> dict[str, _DocEntry]:
    return {d.slug: d for d in _DOCS}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("")
@router.get("/")
async def docs_index(request: Request):
    """Linked-card index of every doc + the CLI reference."""
    return render(
        request,
        "manual/index.html",
        docs=_DOCS,
    )


@router.get("/cli")
async def docs_cli(request: Request):
    """Auto-generated CLI reference page.

    Walks the Typer app's Click command tree and captures the
    ``--help`` output of the root + every subgroup + every leaf
    command. Renders each as a preformatted block with an anchor
    so the in-page TOC can navigate them. This is the same text
    you'd see at the terminal — single source of truth, no
    hand-maintained command index to drift.
    """
    sections = _build_cli_sections()
    return render(
        request,
        "manual/cli.html",
        sections=sections,
    )


@router.get("/{slug}")
async def docs_render(slug: str, request: Request):
    """Render a registered markdown file as HTML."""
    entry = _docs_by_slug().get(slug)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Unknown doc: {slug}")
    if not entry.path.exists():
        log.warning("docs: %s missing on disk at %s", slug, entry.path)
        raise HTTPException(
            status_code=404,
            detail=f"Doc file not found on disk: {entry.filename}",
        )

    raw = entry.path.read_text(encoding="utf-8")
    html_body, toc = _render_markdown(raw)
    return render(
        request,
        "manual/render.html",
        entry=entry,
        body=Markup(html_body),
        toc=Markup(toc),
        all_docs=_DOCS,
    )


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def _render_markdown(raw: str) -> tuple[str, str]:
    """Render markdown to HTML body + table-of-contents fragment.

    Uses these ``markdown`` extensions:
      * ``fenced_code`` — GFM-style ``` code blocks ```.
      * ``tables``     — GFM pipe tables.
      * ``toc``        — anchors on every heading + a TOC fragment.
      * ``codehilite`` — Pygments span classes on code blocks.
        (Visual styling stays minimal — Tailwind handles the rest.)
      * ``sane_lists`` — better behavior on adjacent ordered/unordered
        lists.

    Returns ``(body_html, toc_html)``. Both are HTML strings; the
    template wraps them in :class:`Markup` to bypass auto-escaping.
    """
    md = markdown_lib.Markdown(
        extensions=[
            "fenced_code",
            "tables",
            "toc",
            "sane_lists",
        ],
        extension_configs={
            "toc": {
                "toc_depth": "2-4",  # H2 + H3 + H4 — H1 is the page title
                "permalink": False,
                "anchorlink": True,  # heading itself is the anchor link
            },
        },
    )
    body = md.convert(raw)
    toc = md.toc  # populated by the toc extension after convert()
    return body, toc


# ---------------------------------------------------------------------------
# CLI introspection
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _CliSection:
    """One renderable block on the CLI reference page."""

    anchor: str  # safe id="" — used by the in-page TOC links
    name: str  # human-friendly path, e.g. "stockscan signals backfill"
    level: int  # 1 = root, 2 = group, 3 = leaf command
    help_text: str  # raw `--help` capture
    is_group: bool


def _build_cli_sections() -> list[_CliSection]:
    """Walk the Typer app and capture --help for each command.

    Implementation note: Typer's recent versions render help via Rich
    and emit it through stdout rather than returning it from
    ``Command.get_help()``. We use :class:`typer.testing.CliRunner` to
    invoke each command with ``--help`` and capture the rendered
    output cleanly. CliRunner sets a fixed terminal width and strips
    ANSI codes — exactly what we want for HTML embedding.

    Lazy-imports the CLI module so the FastAPI app boot doesn't pay
    the introspection cost.
    """
    try:
        from typer.testing import CliRunner

        from stockscan.cli import app as cli_app
    except Exception as exc:
        log.warning("docs/cli: failed to import stockscan.cli: %s", exc)
        return []

    try:
        root_cmd = typer.main.get_command(cli_app)
    except Exception as exc:
        log.warning("docs/cli: typer get_command failed: %s", exc)
        return []

    runner = CliRunner()
    sections: list[_CliSection] = []

    # Root: stockscan --help
    sections.append(
        _CliSection(
            anchor="stockscan",
            name="stockscan",
            level=1,
            help_text=_capture_help(runner, cli_app, []),
            is_group=isinstance(root_cmd, click.Group),
        )
    )

    if not isinstance(root_cmd, click.Group):
        return sections

    # Walk subcommands. Two-level nesting is enough for the current
    # CLI shape (root → group → leaf). Any deeper groupings would
    # need a recursive walk.
    for sub_name in sorted(root_cmd.commands.keys()):
        sub = root_cmd.commands[sub_name]
        sections.append(
            _CliSection(
                anchor=f"stockscan-{sub_name}",
                name=f"stockscan {sub_name}",
                level=2,
                help_text=_capture_help(runner, cli_app, [sub_name]),
                is_group=isinstance(sub, click.Group),
            )
        )
        if isinstance(sub, click.Group):
            for leaf_name in sorted(sub.commands.keys()):
                sections.append(
                    _CliSection(
                        anchor=f"stockscan-{sub_name}-{leaf_name}",
                        name=f"stockscan {sub_name} {leaf_name}",
                        level=3,
                        help_text=_capture_help(
                            runner, cli_app, [sub_name, leaf_name]
                        ),
                        is_group=False,
                    )
                )

    return sections


def _capture_help(runner: object, cli_app: object, args: list[str]) -> str:
    """Invoke ``stockscan {args} --help`` via CliRunner and return its output.

    Implementation details:

      * ``prog_name="stockscan"`` so the rendered "Usage: ..." line
        reads "stockscan signals backfill ..." instead of CliRunner's
        default "root signals backfill ...".
      * ``env={"TERMINAL_WIDTH": "100", "COLUMNS": "100"}`` widens the
        Rich console so command tables don't word-wrap mid-cell. The
        ``<pre>`` block in the template scrolls horizontally if needed.
      * Soft-fails to a placeholder string on any error so one broken
        command can't blank out the whole CLI reference page.
    """
    try:
        result = runner.invoke(  # type: ignore[attr-defined]
            cli_app,
            [*args, "--help"],
            prog_name="stockscan",
            env={"TERMINAL_WIDTH": "100", "COLUMNS": "100"},
        )
    except Exception as exc:
        log.warning("docs/cli: invoke failed for %s: %s", args, exc)
        return f"(help unavailable: {exc})"
    if result.exit_code not in (0, 2):
        # exit_code=2 is normal for `--help` in some Typer paths (showing
        # help and exiting). Anything else is a real failure.
        log.warning(
            "docs/cli: --help for %s exited with code %s",
            args,
            result.exit_code,
        )
    output = (result.stdout or "").strip("\n")
    return output or "(no help text)"
