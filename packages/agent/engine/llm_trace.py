"""Opt-in LLM request/response tracing (contextvar sink, zero-cost when unset).

The research auto-run sets a file sink at job entry (``apps/worker/tasks.py``) so every
LLM call — the agent loop's full ``request``/``response`` AND the REVIEW stage's internal
reviewer prompts — is appended as one JSON line to the task's ``llm_trace.jsonl`` for
post-hoc stall analysis. When no sink is bound (every other caller), :func:`emit` is a
no-op; a sink error NEVER propagates into the run.
"""
from __future__ import annotations

import contextvars
import json
from datetime import datetime, timezone
from typing import Any, Callable

_sink: contextvars.ContextVar[Callable[[dict], None] | None] = contextvars.ContextVar(
    "llm_trace_sink", default=None
)


def set_llm_trace_sink(sink: Callable[[dict], None] | None) -> None:
    _sink.set(sink)


def get_llm_trace_sink() -> Callable[[dict], None] | None:
    return _sink.get()


def emit(entry: dict[str, Any]) -> None:
    sink = _sink.get()
    if sink is None:
        return
    try:
        sink({"ts": datetime.now(timezone.utc).isoformat(), **entry})
    except Exception:  # noqa: BLE001 - tracing is observation, never a failure mode
        pass


def jsonl_writer(path):
    """A sink that appends each entry as one UTF-8 JSON line to ``path``."""
    from pathlib import Path

    p = Path(path)

    def _write(entry: dict) -> None:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8", newline="") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")

    return _write
