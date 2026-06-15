"""Central logging configuration — the single place handlers are wired.

Every module in the codebase does ``log = logging.getLogger(__name__)`` and
nothing else; this module is where those records actually gain a formatter
and destinations. Call :func:`setup_logging` once per process entrypoint:

    from stockscan.logging_setup import setup_logging
    setup_logging(component="web")      # web/app.py
    setup_logging(component="cli")      # cli.py app callback
    setup_logging(component="nightly")  # jobs entrypoint (via cli callback)

Behavior:

- **Console** (stderr): single-line human-readable format with timestamp,
  level, and logger name. Always on.
- **Rotating file**: ``<log_dir>/stockscan-<component>.log`` (5 MB × 5
  backups). On by default; disabled when ``settings.log_to_file`` is false
  or ``settings.env == "test"`` so the test suite never writes files.
  Per-component filenames keep web request logs out of nightly-job logs.
- **Library noise**: httpx/httpcore/uvicorn.access etc. are pinned to
  WARNING so INFO stays signal, not handshake chatter.

Idempotent: calling twice with the same component is a no-op; calling with
a *different* component swaps the file handler (the CLI callback configures
``cli`` first, then the ``jobs`` subcommand re-targets to ``nightly`` so
scheduled runs land in their own file).
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

from stockscan.config import settings

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s · %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

MAX_BYTES = 5 * 1024 * 1024  # 5 MB per file
BACKUP_COUNT = 5

# Libraries whose INFO/DEBUG output is noise at app level.
_QUIET_LIBRARIES = (
    "httpx",
    "httpcore",
    "urllib3",
    "asyncio",
    "matplotlib",
    "uvicorn.access",  # superseded by our own request-timing middleware
)

# Module-level state for idempotency. Tracks which component the current
# file handler belongs to (None = not yet configured).
_configured_component: str | None = None
_file_handler: logging.Handler | None = None


def setup_logging(
    *,
    component: str = "app",
    level: str | None = None,
    log_dir: str | Path | None = None,
) -> None:
    """Configure root logging for this process. Safe to call repeatedly.

    Args:
        component: short tag used in the log filename
            (``stockscan-<component>.log``). Convention: ``web``, ``cli``,
            ``nightly``.
        level: log level name; defaults to ``settings.log_level``.
        log_dir: directory for rotating files; defaults to
            ``settings.resolved_log_dir``. Created if missing.
    """
    global _configured_component, _file_handler

    root = logging.getLogger()
    resolved_level = (level or settings.log_level or "INFO").upper()

    if _configured_component is None:
        # First call — wire console handler + level + library quieting.
        root.setLevel(resolved_level)
        console = logging.StreamHandler(stream=sys.stderr)
        console.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))
        root.addHandler(console)
        for lib in _QUIET_LIBRARIES:
            logging.getLogger(lib).setLevel(logging.WARNING)
    elif _configured_component == component:
        return  # already configured for this component — no-op

    # (Re)attach the rotating file handler for this component.
    if _file_handler is not None:
        root.removeHandler(_file_handler)
        _file_handler.close()
        _file_handler = None

    if settings.log_to_file and not settings.is_test:
        directory = Path(log_dir) if log_dir is not None else settings.resolved_log_dir
        try:
            directory.mkdir(parents=True, exist_ok=True)
            handler = logging.handlers.RotatingFileHandler(
                directory / f"stockscan-{component}.log",
                maxBytes=MAX_BYTES,
                backupCount=BACKUP_COUNT,
                encoding="utf-8",
            )
            handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))
            root.addHandler(handler)
            _file_handler = handler
        except OSError as exc:
            # A read-only filesystem or bad path must never take the app
            # down — console logging still works; say so once and move on.
            logging.getLogger(__name__).warning(
                "file logging disabled — cannot write to %s: %s", directory, exc
            )

    _configured_component = component


def reset_logging() -> None:
    """Tear down handlers installed by :func:`setup_logging` (test helper)."""
    global _configured_component, _file_handler
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    _configured_component = None
    _file_handler = None
