"""Observability foundation for the agent: trace context, per-turn spans, audit trail.

Three layers:

- :class:`TraceContext` — contextvars for ``trace_id`` / ``turn_id`` / ``user_id`` /
  ``session_id``, so logs and spans are correlatable across the API, worker, and tools.
- :class:`TurnSpan` — one per agent turn; records steps, tool calls, LLM latency, errors
  and an estimated cost. This is what the API reads for usage/`cost_usd`.
- :class:`AuditSink` — appends one JSONL line per turn to an audit file (the audit trail
  the agent loop previously never produced).

Logging uses structlog (structured KV logs). Every log call carries the trace snapshot.
"""
from __future__ import annotations

import json
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

_log = structlog.get_logger("deepdive.agent")

# ── trace context ──
_trace_id: ContextVar[str] = ContextVar("trace_id", default="")
_turn_id: ContextVar[str] = ContextVar("turn_id", default="")
_user_id: ContextVar[str] = ContextVar("user_id", default="")
_session_id: ContextVar[str] = ContextVar("session_id", default="")


class TraceContext:
    """Contextvar-backed correlation ids for one request/turn chain."""

    @classmethod
    def bind(cls, *, trace_id: str = "", turn_id: str = "", user_id: str = "", session_id: str = "") -> None:
        if trace_id:
            _trace_id.set(trace_id)
        if turn_id:
            _turn_id.set(turn_id)
        if user_id:
            _user_id.set(user_id)
        if session_id:
            _session_id.set(session_id)

    @classmethod
    def snapshot(cls) -> dict[str, str]:
        return {
            "trace_id": _trace_id.get(),
            "turn_id": _turn_id.get(),
            "user_id": _user_id.get(),
            "session_id": _session_id.get(),
        }


# Default price map (USD per 1M tokens, prompt/completion). Overridable per model; a model
# not in the map is priced at 0 so cost estimates are conservative rather than wrong.
_PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "deepdive-chat": (0.15, 0.60),
}


def estimate_cost_usd(usage: dict, model: str | None) -> float:
    """Rough USD cost of a token-count dict for ``model`` (0 when unknown/absent)."""
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    if not prompt and not completion:
        return 0.0
    p, c = _PRICES_PER_MTOK.get(model or "", (0.0, 0.0))
    return round(prompt / 1_000_000 * p + completion / 1_000_000 * c, 6)


@dataclass
class TurnSpan:
    """Per-turn observability record. Started by the kernel, finished at turn end."""

    turn_id: str
    started_at: float = field(default_factory=time.monotonic)
    finished_at: float | None = None
    steps: list[dict] = field(default_factory=list)
    tools: list[dict] = field(default_factory=list)
    llm_calls: int = 0
    errors: list[dict] = field(default_factory=list)
    cost_usd: float = 0.0

    def record_step(self, *, index: int, tool_calls: int, tokens: int, duration_ms: float) -> None:
        self.steps.append(
            {
                "index": index,
                "tool_calls": tool_calls,
                "tokens": tokens,
                "duration_ms": round(duration_ms, 1),
            }
        )

    def record_tool(self, *, name: str, is_error: bool, duration_ms: float) -> None:
        self.tools.append(
            {"name": name, "is_error": is_error, "duration_ms": round(duration_ms, 1)}
        )

    def record_llm(self, duration_ms: float) -> None:
        self.llm_calls += 1
        if self.steps:
            self.steps[-1]["llm_duration_ms"] = round(duration_ms, 1)

    def record_error(self, *, kind: str, message: str) -> None:
        self.errors.append({"kind": kind, "message": str(message)[:500]})

    def finish(self, *, cost_usd: float = 0.0) -> None:
        self.finished_at = time.monotonic()
        self.cost_usd = round(cost_usd, 6)

    @property
    def duration_s(self) -> float:
        end = self.finished_at or time.monotonic()
        return round(end - self.started_at, 3)

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "duration_s": self.duration_s,
            "steps": len(self.steps),
            "llm_calls": self.llm_calls,
            "tools": self.tools,
            "errors": self.errors,
            "cost_usd": self.cost_usd,
            "tokens": sum(s.get("tokens", 0) for s in self.steps),
        }


class AuditSink:
    """Appends one JSONL line per finished turn to an audit file (best-effort)."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path

    def write(self, payload: dict) -> None:
        if self.path is None:
            _log.info("audit", **payload)
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
        except OSError:
            _log.warning("audit_sink_write_failed")


def log_event(type_: str, **kw: Any) -> None:
    """Generic structured event log (``agent.{type_}``), carrying the trace snapshot."""
    _log.info(f"agent.{type_}", **{**TraceContext.snapshot(), **kw})


def log_step(**kw: Any) -> None:
    _log.info("agent.step", **{**TraceContext.snapshot(), **kw})


def log_tool(**kw: Any) -> None:
    _log.info("agent.tool", **{**TraceContext.snapshot(), **kw})


def log_llm(**kw: Any) -> None:
    _log.info("agent.llm", **{**TraceContext.snapshot(), **kw})


def log_error(**kw: Any) -> None:
    _log.error("agent.error", **{**TraceContext.snapshot(), **kw})
