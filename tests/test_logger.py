"""Tests for core.logger: concurrency-safe context tagging, capacity_warning
levels/format, idempotent per-app configure_logging, and the uvicorn.access
research-list-poll silencer.

These tests attach capture handlers to the root logger (or a child logger) and restore the
previous root state afterwards, so they never leak handlers into sibling tests.
"""
import asyncio
import logging
import logging.handlers
from pathlib import Path
from types import SimpleNamespace

import core.logger as logger_module
import pytest
from core.logger import (
    ContextFilter,
    capacity_warning,
    configure_logging,
    reset_log_context,
    set_log_context,
)


class _Capture(logging.Handler):
    """Collect emitted records (level NOTSET so every record reaches it)."""

    def __init__(self) -> None:
        super().__init__(level=logging.NOTSET)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def clean_logging():
    """Snapshot root state (handlers + level), the core.logger globals, and the
    uvicorn.access filters; restore all after the test."""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    saved_app = logger_module._configured_app
    saved_installed = list(logger_module._installed_handlers)
    access_logger = logging.getLogger("uvicorn.access")
    saved_access_flag = logger_module._access_filter_installed
    saved_access_filters = list(access_logger.filters)
    yield
    for handler in list(root.handlers):
        if handler not in saved_handlers:
            root.removeHandler(handler)
            handler.close()
    logger_module._installed_handlers[:] = saved_installed
    logger_module._configured_app = saved_app
    for filt in list(access_logger.filters):
        if filt not in saved_access_filters:
            access_logger.removeFilter(filt)
    logger_module._access_filter_installed = saved_access_flag
    root.setLevel(saved_level)


def _capture_root() -> tuple[_Capture, list[logging.Handler], int]:
    """Attach a capture handler to the root logger; return (cap, prior_handlers, prior_level)."""
    root = logging.getLogger()
    prior = list(root.handlers)
    prior_level = root.level
    cap = _Capture()
    cap.addFilter(ContextFilter())
    root.addHandler(cap)
    root.setLevel(logging.DEBUG)
    return cap, prior, prior_level


def _restore_root(cap: _Capture, prior: list, prior_level: int) -> None:
    root = logging.getLogger()
    root.removeHandler(cap)
    root.setLevel(prior_level)
    # Drop any capture children we may have added; prior list is authoritative.
    for handler in list(root.handlers):
        if handler not in prior and handler is not cap:
            root.removeHandler(handler)


def _record_fields(cap: _Capture) -> list[dict]:
    return [
        {
            "user_id": r.user_id,
            "session_id": r.session_id,
            "request_id": r.request_id,
            "task_id": r.task_id,
            "run_id": r.run_id,
            "level": r.levelname,
            "msg": r.getMessage(),
        }
        for r in cap.records
    ]


async def test_concurrent_session_context_does_not_crosstalk(clean_logging):
    """N tasks each tag their own session_id; no record ever sees a sibling's tag."""
    cap, prior, prior_level = _capture_root()
    logger = logging.getLogger("t.crosstalk")
    try:

        async def emit(tag: str) -> None:
            tokens = set_log_context(session_id=f"session-{tag}", user_id=f"user-{tag}")
            try:
                for _ in range(30):
                    logger.info("hello %s", tag)
                    await asyncio.sleep(0)  # let sibling tasks interleave
            finally:
                reset_log_context(tokens)

        await asyncio.gather(*(emit(str(i)) for i in range(12)))
    finally:
        _restore_root(cap, prior, prior_level)

    assert len(cap.records) == 12 * 30
    for record in cap.records:
        tag = record.getMessage().split()[1]
        assert record.session_id == f"session-{tag}"
        assert record.user_id == f"user-{tag}"


async def test_reset_restores_default_dash(clean_logging):
    """After reset_log_context, a fresh record in the same task carries the "-" default."""
    cap, prior, prior_level = _capture_root()
    logger = logging.getLogger("t.reset")
    try:
        tokens = set_log_context(session_id="abc", task_id="task-1", run_id="run-9")
        logger.info("tagged")
        reset_log_context(tokens)
        logger.info("untagged")
    finally:
        _restore_root(cap, prior, prior_level)

    fields = _record_fields(cap)
    assert fields[0]["session_id"] == "abc"
    assert fields[0]["task_id"] == "task-1"
    assert fields[0]["run_id"] == "run-9"
    # Second record emitted after reset sees defaults again.
    assert fields[1]["session_id"] == "-"
    assert fields[1]["task_id"] == "-"
    assert fields[1]["run_id"] == "-"
    assert fields[1]["user_id"] == "-"


def test_configure_logging_silences_research_list_access_log(tmp_path, clean_logging):
    """The API configure installs a filter on uvicorn.access (propagate=False) that drops
    the desktop's collection GET /research/tasks poll but keeps every other line."""
    settings = _stub_settings(tmp_path)
    configure_logging(settings, app="api")

    access_logger = logging.getLogger("uvicorn.access")
    prior_level = access_logger.level
    prior_propagate = access_logger.propagate
    cap = _Capture()
    try:
        access_logger.setLevel(logging.INFO)
        access_logger.propagate = False  # mimic uvicorn's real access logger
        access_logger.addHandler(cap)
        # The renderer calls /api/research/tasks; the Electron/Vite proxy strips /api, so the
        # backend's access line is the bare /research/tasks (exactly what gets polled).
        access_logger.info('127.0.0.1:1 - "GET /research/tasks HTTP/1.1" 200')
        access_logger.info('127.0.0.1:2 - "GET /research/tasks?limit=5 HTTP/1.1" 200')
        access_logger.info('127.0.0.1:3 - "GET /research/tasks/abc-123 HTTP/1.1" 200')
        access_logger.info('127.0.0.1:4 - "GET /research/tasks/abc-123/monitor HTTP/1.1" 200')
        access_logger.info('127.0.0.1:5 - "POST /research/tasks HTTP/1.1" 201')
        access_logger.info('127.0.0.1:6 - "GET /sessions HTTP/1.1" 200')
    finally:
        access_logger.removeHandler(cap)
        access_logger.setLevel(prior_level)
        access_logger.propagate = prior_propagate

    msgs = [r.getMessage() for r in cap.records]
    # The bare-collection poll (with or without a query string) is silenced.
    assert not any('"GET /research/tasks HTTP' in m or '"GET /research/tasks?' in m for m in msgs)
    # Detail, monitor, create and unrelated endpoints still reach the access log.
    assert len(msgs) == 4
    assert any("/monitor" in m for m in msgs)
    assert any('"POST /research/tasks HTTP' in m for m in msgs)
    assert any('"GET /sessions HTTP' in m for m in msgs)


def test_capacity_warning_level_and_format(clean_logging):
    cap, prior, prior_level = _capture_root()
    cap_logger = logging.getLogger("deepdive.capacity")
    saved_level = cap_logger.level
    cap_logger.setLevel(logging.DEBUG)
    try:
        capacity_warning("queue", current=8, limit=10, message="approaching cap")
        capacity_warning("queue", current=10, limit=10, message="at cap")
        capacity_warning("queue", current=12, limit=None, message="unbounded current")
    finally:
        cap_logger.setLevel(saved_level)
        _restore_root(cap, prior, prior_level)

    records = [r for r in cap.records if r.name == "deepdive.capacity"]
    assert [r.levelname for r in records] == ["WARNING", "ERROR", "WARNING"]
    assert "component=queue" in records[0].getMessage()
    assert "at cap" in records[1].getMessage()
    # With no limit there is no cap to trip, so the raw message is emitted as a warning.
    assert "unbounded current" in records[2].getMessage()


def _stub_settings(tmp_path: Path, level: str = "INFO") -> SimpleNamespace:
    return SimpleNamespace(
        log_level=level,
        log_dir=tmp_path,
        log_file_max_bytes=10 * 1024 * 1024,
        log_file_backups=2,
    )


def test_configure_logging_is_idempotent_and_splits_files(tmp_path, clean_logging):
    settings = _stub_settings(tmp_path)
    root = logging.getLogger()

    configure_logging(settings, app="api")
    api_file_handlers = [h for h in root.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
    assert len(api_file_handlers) == 1
    assert api_file_handlers[0].baseFilename.endswith("api.log")

    # Second call for the same app is a no-op: no duplicate handlers.
    configure_logging(settings, app="api")
    api_file_handlers = [h for h in root.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
    assert len(api_file_handlers) == 1

    # Switching to "worker" replaces the file handler (API log handler closed).
    configure_logging(settings, app="worker")
    file_handlers = [h for h in root.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
    assert len(file_handlers) == 1
    assert file_handlers[0].baseFilename.endswith("worker.log")

    # Both emit to their own file.
    root.info("api-ish message")
    assert (tmp_path / "api.log").exists()
    assert (tmp_path / "worker.log").exists()


def test_configure_logging_rotates(tmp_path, clean_logging):
    small = SimpleNamespace(
        log_level="INFO",
        log_dir=tmp_path,
        log_file_max_bytes=256,
        log_file_backups=1,
    )
    root = logging.getLogger()
    configure_logging(small, app="rot")
    # Two full rotations' worth of content forces at least one backup.
    blob = "z" * 200 + "\n"
    for _ in range(12):
        root.info(blob.rstrip())
    (rot_file,) = [h.baseFilename for h in root.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
    rot_path = Path(rot_file)
    assert rot_path.exists()
    assert (tmp_path / "rot.log.1").exists()
    # The active file never exceeds the cap by more than one line.
    assert rot_path.stat().st_size <= 256 + len(blob)


def test_log_record_fields_stamped_by_formatter_context(tmp_path, clean_logging):
    """A record formatted after configure_logging carries the log-context columns."""
    root = logging.getLogger()
    settings = _stub_settings(tmp_path)
    configure_logging(settings, app="fmt")
    tokens = set_log_context(user_id="u", session_id="s", task_id="t", run_id="r")
    try:
        root.info("hello")
    finally:
        reset_log_context(tokens)
    lines = (tmp_path / "fmt.log").read_text(encoding="utf-8").splitlines()
    assert lines, "expected at least one formatted log line"
    assert "[u][s][" in lines[0]
    assert "[t:r]" in lines[0]
