"""Tests for stockscan.logging_setup + the web request-timing middleware."""

from __future__ import annotations

import logging

import pytest

from stockscan import logging_setup
from stockscan.config import settings
from stockscan.logging_setup import reset_logging, setup_logging


@pytest.fixture(autouse=True)
def _clean_logging_state():
    """Each test starts and ends with no stockscan-installed handlers."""
    reset_logging()
    yield
    reset_logging()


def _ours(handler: logging.Handler) -> bool:
    """True for handlers installed by setup_logging (pytest adds its own)."""
    fmt = getattr(handler.formatter, "_fmt", None)
    return fmt == logging_setup.LOG_FORMAT


def _console_handlers() -> list[logging.Handler]:
    root = logging.getLogger()
    return [
        h
        for h in root.handlers
        if _ours(h) and not isinstance(h, logging.handlers.RotatingFileHandler)
    ]


def test_setup_is_idempotent_per_component() -> None:
    setup_logging(component="cli")
    first = list(logging.getLogger().handlers)
    setup_logging(component="cli")
    assert logging.getLogger().handlers == first


def test_no_file_handler_in_test_env() -> None:
    # conftest pins STOCKSCAN_ENV=test, so file logging must stay off even
    # though log_to_file defaults to true.
    assert settings.is_test
    setup_logging(component="cli")
    root = logging.getLogger()
    assert not any(
        isinstance(h, logging.handlers.RotatingFileHandler) for h in root.handlers
    )
    assert len(_console_handlers()) == 1


def test_file_handler_written_outside_test_env(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "env", "dev")
    setup_logging(component="cli", log_dir=tmp_path)
    logging.getLogger("stockscan.test").info("hello file")
    logging.shutdown()
    logfile = tmp_path / "stockscan-cli.log"
    assert logfile.exists()
    assert "hello file" in logfile.read_text(encoding="utf-8")


def test_component_switch_swaps_file_handler(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "env", "dev")
    setup_logging(component="cli", log_dir=tmp_path)
    setup_logging(component="nightly", log_dir=tmp_path)
    root = logging.getLogger()
    file_handlers = [
        h for h in root.handlers if isinstance(h, logging.handlers.RotatingFileHandler)
    ]
    # Exactly one file handler, pointing at the nightly file.
    assert len(file_handlers) == 1
    assert file_handlers[0].baseFilename.endswith("stockscan-nightly.log")
    # Console handler was NOT duplicated by the re-configure.
    assert len(_console_handlers()) == 1


def test_unwritable_log_dir_degrades_gracefully(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "env", "dev")
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory")  # mkdir under a file → OSError
    setup_logging(component="cli", log_dir=blocked / "sub")
    # No file handler, but console logging still configured — app survives.
    root = logging.getLogger()
    assert not any(
        isinstance(h, logging.handlers.RotatingFileHandler) for h in root.handlers
    )
    assert len(_console_handlers()) == 1


def test_reset_logging_clears_state() -> None:
    setup_logging(component="cli")
    reset_logging()
    assert logging_setup._configured_component is None
    assert _console_handlers() == []


# ----------------------------------------------------------------------
# Request-timing middleware
# ----------------------------------------------------------------------


def test_request_timing_header_and_log(caplog) -> None:
    from fastapi.testclient import TestClient

    from stockscan.web.app import app

    client = TestClient(app)
    with caplog.at_level(logging.INFO, logger="stockscan.web.app"):
        resp = client.get("/strategies")
    assert "X-Response-Time" in resp.headers
    assert resp.headers["X-Response-Time"].endswith("ms")
    timing_lines = [
        r.message for r in caplog.records if "GET /strategies ->" in r.message
    ]
    assert timing_lines, "expected a timing log line for the request"


def test_health_requests_log_at_debug_only(caplog) -> None:
    from fastapi.testclient import TestClient

    from stockscan.web.app import app

    client = TestClient(app)
    with caplog.at_level(logging.INFO, logger="stockscan.web.app"):
        client.get("/health")
    assert not any(
        "GET /health ->" in r.message
        for r in caplog.records
        if r.levelno >= logging.INFO
    )
