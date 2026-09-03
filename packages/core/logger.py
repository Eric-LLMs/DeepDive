"""Concurrency-safe runtime logging for the API and worker processes.

A single configuration point for the whole process's stdlib logging: every existing
``logging.getLogger(__name__)`` call site inherits the root handlers configured here, so no
business logger needs touching. Each log line is tagged with the active per-request / per-job
context — ``user_id``, ``session_id``, ``request_id``, ``task_id:run_id`` — read from
:class:`~contextvars.ContextVar` fields by :class:`ContextFilter`. That is safe under asyncio: a
request or a worker
job runs in its own task context, sets the fields it knows, and resets them when it finishes,
so two concurrent sessions never read each other's fields.

Deliberately **not** named ``logging.py``: that would shadow the stdlib ``logging`` package for
sibling modules and break every ``import logging`` in the tree.

File layout: one rotating file per process role (``logs/api.log`` and ``logs/worker.log``), so
the API and worker processes never contend for the same file handle. The stream handler mirrors
the same records to the terminal, colourised by level when the stream is a TTY.

One lightweight diagnostic lives here too — ``capacity_warning`` (WARNING/ERROR) — for capacity
guardrails. It does not replace ``agent.engine.telemetry`` (structured, persisted events) nor the
request audit trail; it is meant for grepping a log file in the moment.
"""
from __future__ import annotations

import contextvars
import logging
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

# Context fields copied onto every log record by ContextFilter. The "-" default keeps a log line
# emitted outside any request/job (startup, cron, tests) stable and greppable.
_FIELD_DEFAULTS: dict[str, str] = {
    "user_id": "-",
    "session_id": "-",
    "request_id": "-",
    "task_id": "-",
    "run_id": "-",
}
_CONTEXTS: dict[str, contextvars.ContextVar[str]] = {
    name: contextvars.ContextVar(f"log_{name}", default=default)
    for name, default in _FIELD_DEFAULTS.items()
}

# Terminal colour per level; disabled when the stream is not a TTY (files never see ANSI).
_LEVEL_COLORS = {
    "DEBUG": "\x1b[36m",     # cyan
    "INFO": "\x1b[32m",      # green
    "WARNING": "\x1b[33m",   # yellow
    "ERROR": "\x1b[31m",     # red
    "CRITICAL": "\x1b[1;41m",  # bold on red background
}
_RESET = "\x1b[0m"

_LOG_FORMAT = (
    "[%(asctime)s][%(levelname)s][%(user_id)s][%(session_id)s][%(request_id)s]"
    "[%(task_id)s:%(run_id)s][%(name)s:%(lineno)d] %(message)s"
)
_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"

class ContextFilter(logging.Filter):
    """Stamp the active log context onto every record, overriding any stale attribute.

    Runs inside the caller's task context, so ``record.session_id`` etc. always reflect the
    session that *emitted* the line — never a sibling coroutine's.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        for name, var in _CONTEXTS.items():
            setattr(record, name, var.get())
        return True


class _LogFormatter(logging.Formatter):
    """Plain formatter used by the file handler."""

    def __init__(self) -> None:
        super().__init__(fmt=_LOG_FORMAT, datefmt=_DATETIME_FORMAT)


class _StreamFormatter(_LogFormatter):
    """File layout plus ANSI colour on the level name — only when the stream is a TTY."""

    def __init__(self, stream: Any) -> None:
        super().__init__()
        try:
            self._colour = bool(stream is not None and stream.isatty())
        except (AttributeError, ValueError):
            self._colour = False

    def format(self, record: logging.LogRecord) -> str:
        colour = _LEVEL_COLORS.get(record.levelname)
        if not (self._colour and colour):
            return super().format(record)
        original = record.levelname
        record.levelname = f"{colour}{original}{_RESET}"
        try:
            return super().format(record)
        finally:
            record.levelname = original


def set_log_context(**kwargs: Any) -> dict[str, contextvars.Token]:
    """Set one or more log-context fields for the current task.

    Accepts any of ``user_id`` / ``session_id`` / ``request_id`` / ``task_id`` / ``run_id``.
    Returns ``{field: token}`` for the fields that were set — pass it to
    :func:`reset_log_context` in a ``finally`` so a long-lived task (worker job, SSE stream)
    never leaks its context into the next unit of work.
    """
    tokens: dict[str, contextvars.Token] = {}
    for name, value in kwargs.items():
        var = _CONTEXTS.get(name)
        if var is not None and value is not None:
            tokens[name] = var.set(str(value))
    return tokens


def reset_log_context(tokens: dict[str, contextvars.Token] | None) -> None:
    """Restore the previous values of fields set by :func:`set_log_context`."""
    if not tokens:
        return
    for name, token in tokens.items():
        var = _CONTEXTS.get(name)
        if var is not None:
            try:
                var.reset(token)
            except ValueError:
                # The token is already reset (e.g. the owning task ended) — nothing to undo.
                pass


# Messages whose ``request_line`` matches the desktop research chip poll / sidebar list refresh.
# The renderer calls ``/api/research/tasks``; the Electron (and Vite) proxy strips the ``/api``
# prefix, so the backend sees the bare collection ``GET /research/tasks`` fired on a timer. It is
# dropped from uvicorn's access log so the 10-second poll does not drown out real request lines.
_CHIP_POLL_MARKERS = ("GET /research/tasks HTTP", "GET /research/tasks?")


class _QuietResearchListFilter(logging.Filter):
    """Drop uvicorn.access records for the desktop ``GET /research/tasks`` list poll.

    ``uvicorn.access`` has ``propagate=False``, so a root-level filter never sees its records;
    the filter must attach to that logger directly. Only the bare collection GET is silenced —
    task detail, monitor SSE, creates, deletes and every other endpoint stay logged.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return not any(marker in message for marker in _CHIP_POLL_MARKERS)


_configure_lock = threading.Lock()
_configured_app: str | None = None
_installed_handlers: list[logging.Handler] = []
_access_filter_installed = False


def configure_logging(settings: Any, app: str = "api") -> None:
    """Attach the process's stream + rotating-file handlers to the root logger.

    Idempotent: calling it again for the same ``app`` is a no-op (no duplicate handlers). A
    different ``app`` (e.g. a test that configures ``api`` then ``worker``) replaces the file
    handler so each role keeps its own log file. ``settings`` needs ``log_level`` / ``log_dir``
    / ``log_file_max_bytes`` / ``log_file_backups`` — pass ``core.config.settings``.

    For ``app == "api"`` the ``uvicorn.access`` logger (which never propagates to the root
    handlers) also gets a filter that silences the desktop research list poll (see
    :class:`_QuietResearchListFilter`).
    """
    global _configured_app, _access_filter_installed
    if app == "api" and not _access_filter_installed:
        logging.getLogger("uvicorn.access").addFilter(_QuietResearchListFilter())
        _access_filter_installed = True
    with _configure_lock:
        if _configured_app == app:
            return
        root = logging.getLogger()
        for handler in _installed_handlers:
            root.removeHandler(handler)
            handler.close()
        _installed_handlers.clear()
        try:
            level = logging.getLevelName(str(settings.log_level).upper())
        except (AttributeError, TypeError):
            level = logging.INFO
        if not isinstance(level, int):
            level = logging.INFO
        root.setLevel(level)

        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(_StreamFormatter(sys.stderr))
        stream.addFilter(ContextFilter())
        root.addHandler(stream)
        _installed_handlers.append(stream)

        log_dir = Path(getattr(settings, "log_dir", "logs"))
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            log_dir = Path("logs")
            log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_dir / f"{app}.log",
            maxBytes=int(getattr(settings, "log_file_max_bytes", 10 * 1024 * 1024)),
            backupCount=int(getattr(settings, "log_file_backups", 5)),
            encoding="utf-8",
        )
        file_handler.setFormatter(_LogFormatter())
        file_handler.addFilter(ContextFilter())
        root.addHandler(file_handler)
        _installed_handlers.append(file_handler)
        _configured_app = app


# Diagnostics logger kept at root level + propagate so it lands in api.log / worker.log.
_capacity_logger = logging.getLogger("deepdive.capacity")


def capacity_warning(component: str, current: Any, limit: Any, message: str) -> None:
    """Warn when a resource approaches its cap; error once it is at or over the cap.

    A single call site so capacity trips are uniform: ERROR (needs attention) at/over the limit,
    WARNING (approaching) below it.
    """
    at_cap = _as_number(current) >= _as_number(limit) if limit is not None else False
    level = logging.ERROR if at_cap else logging.WARNING
    _capacity_logger.log(
        level,
        "capacity component=%s current=%s limit=%s message=%s",
        component,
        current,
        limit,
        message,
    )


def _as_number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("inf") if value is not None else float("-inf")
