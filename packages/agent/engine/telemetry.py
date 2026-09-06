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
import re
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
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


# ── per-turn pricing channel ──
# The caller that resolves an LLM channel (e.g. the worker task that looked the model's
# catalog price up) sets this around a ``kernel.run`` call; :class:`AgentTurn` picks it up at
# turn construction (see ``ReactLoopAgent._ensure_turn``). Same request-scoped pattern as
# ``set_request_llm_channel`` — the generic runtime stays free of any DB/billing dependency
# and carries only a plain numeric pair.
_CURRENT_PRICING: ContextVar[tuple[Any, Any] | None] = ContextVar(
    "agent_current_pricing", default=None
)


def set_current_pricing(pricing: tuple[Any, Any] | None) -> None:
    """Pin ``(prompt_per_1k, completion_per_1k)`` for turns created in this context."""
    _CURRENT_PRICING.set(pricing)


def get_current_pricing() -> tuple[Any, Any] | None:
    return _CURRENT_PRICING.get()


# Fallback price map (USD per 1M tokens, prompt/completion) for models with no authoritative
# price injected. This is a LAST-RESORT builtin, not the source of truth: the authoritative
# per-channel price is resolved upstream (the model catalog) and injected as
# ``AgentTurn.pricing`` (per-1k pair). Do NOT grow this table into a second pricing source —
# adding provider families here when a catalog exists would recreate the dual-source-of-truth
# split. A model that matches nothing anywhere yields PRICING_UNKNOWN (``None``), never a
# silent $0 (unknown must stay distinguishable from free).
_PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "deepdive-chat": (0.15, 0.60),
}

# Model names already reported as PRICING_UNKNOWN (warn once per process per model, so a
# long run logs the billing gap without spamming it).
_WARNED_UNKNOWN: set[str] = set()

_DATE_SUFFIX_RE = re.compile(r"-\d{4}-\d{2}-\d{2}$")
_VARIANT_SUFFIX_RE = re.compile(r"-(?:latest|preview|beta|rc\d*|chat|base|instruct|coder|math)$")


def normalize_model_key(model: str) -> str:
    """Canonical lookup key for a model id: lowercase, date/variant suffixes stripped.

    ``Qwen3-Max-2026-01-01`` → ``qwen3-max``; ``gpt-4o-2024-08-06`` → ``gpt-4o``. Stripping
    runs suffix-wise to a fixed point (``gpt-4o-2024-08-06`` never becomes ``gpt-4o-chat``).
    """
    key = (model or "").strip().lower()
    while True:
        stripped = _DATE_SUFFIX_RE.sub("", key)
        stripped = _VARIANT_SUFFIX_RE.sub("", stripped)
        if stripped == key:
            return key
        key = stripped


def resolve_model_pricing(
    model: str | None, pricing: tuple[Any, Any] | None = None
) -> tuple[Any, Any] | None:
    """Resolve a ``(prompt, completion)`` price pair, or ``None`` = PRICING_UNKNOWN.

    Precedence (authoritative first):
    1. ``pricing`` — the per-1k pair injected by whoever resolved the model's channel
       (the catalog is the single source of truth; carried as Decimal or float);
    2. the builtin ``_PRICES_PER_MTOK`` per-1M fallback table, looked up by normalized key
       and then raw key (covers bare and suffixed OpenAI-style names only).
    """
    if pricing is not None:
        return pricing
    if not model:
        return None
    key = normalize_model_key(model)
    pair = _PRICES_PER_MTOK.get(key) or _PRICES_PER_MTOK.get(model.strip())
    return pair


def estimate_cost_usd(
    usage: dict, model: str | None, pricing: tuple[Any, Any] | None = None
) -> float | None:
    """USD cost of a token-count dict for ``model`` (``None`` = PRICING_UNKNOWN).

    Returns ``0.0`` only when there is genuinely nothing to bill (empty usage) or a
    resolved price computes to zero. Tokens were spent but no price resolved → ``None``
    plus a one-time ``pricing_unknown`` warning: downstream MUST NOT coerce that to $0
    (unknown and free are different facts). Per-1k injected prices are computed with the
    same Decimal discipline as wallet billing to avoid float/Decimal mixing.
    """
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    if not prompt and not completion:
        return 0.0
    pair = resolve_model_pricing(model, pricing)
    if pair is None:
        key = (model or "").strip().lower()
        if key not in _WARNED_UNKNOWN:
            _WARNED_UNKNOWN.add(key)
            _log.warning("pricing_unknown", model=model, message=(
                "no authoritative price for model; cost reported as PRICING_UNKNOWN (None), "
                "not $0 — register the model in the catalog or inject turn.pricing"
            ))
        return None
    p, c = pair
    if pricing is not None:
        # Injected pair is per-1k, authoritative (catalog Decimal units).
        prompt_cost = Decimal(prompt) * Decimal(str(p)) / Decimal(1000)
        completion_cost = Decimal(completion) * Decimal(str(c)) / Decimal(1000)
        return float((prompt_cost + completion_cost).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP))
    # Builtin fallback table is per-1M.
    return round(prompt / 1_000_000 * float(p) + completion / 1_000_000 * float(c), 6)


@dataclass
class TurnSpan:
    """Per-turn observability record. Started by the kernel, finished at turn end."""

    turn_id: str
    started_at: float = field(default_factory=time.monotonic)
    finished_at: float | None = None
    steps: list[dict] = field(default_factory=list)
    tools: list[dict] = field(default_factory=list)
    llm_calls: int = 0
    llm_duration_ms: float = 0.0   # accumulated time inside model calls (record_llm)
    tool_duration_ms: float = 0.0  # accumulated time inside tool execution (record_tool)
    errors: list[dict] = field(default_factory=list)
    # ``None`` = PRICING_UNKNOWN (tokens were spent, no price resolved) — kept distinct
    # from ``0.0`` (nothing to bill). Downstream (driver ledger, API usage) must not
    # coerce None into 0.
    cost_usd: float | None = 0.0

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
        self.tool_duration_ms += duration_ms

    def record_llm(self, duration_ms: float) -> None:
        self.llm_calls += 1
        self.llm_duration_ms += duration_ms
        # Tag the most recently recorded step (callers record the step first, then the LLM
        # that produced it) so per-step llm latency stays attached when steps are inspected.
        if self.steps:
            self.steps[-1]["llm_duration_ms"] = round(duration_ms, 1)

    def record_error(self, *, kind: str, message: str) -> None:
        self.errors.append({"kind": kind, "message": str(message)[:500]})

    def finish(self, *, cost_usd: float | None = 0.0) -> None:
        self.finished_at = time.monotonic()
        self.cost_usd = round(cost_usd, 6) if cost_usd is not None else None

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
            "llm_duration_ms": round(self.llm_duration_ms, 1),
            "tool_duration_ms": round(self.tool_duration_ms, 1),
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


def log_error(*, kind: str = "", **kw: Any) -> None:
    # ``kind`` names the error class (e.g. turn_cancelled / llm_fatal). It must NOT be the
    # ``event`` kwarg: structlog's ``_log.error(event_str, event=...)`` collides on the
    # reserved ``event`` keyword (TypeError), which would swallow the log line itself.
    _log.error("agent.error", **{**TraceContext.snapshot(), "kind": kind, **kw})
