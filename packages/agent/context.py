"""Per-turn agent context: the single home for all mutable state of one turn.

Every piece of state that used to live on shared singletons (``AgentKernel._recall_hits``,
``CacheBoundaryAssembler._injected``) moves into an :class:`AgentTurn` created per
``run()``/``run_stream()``. A process-wide ``ContextVar`` binds the current turn so
subsystems (the prompt assembler, tools, the approval bridge) can read turn state without
the kernel passing it through every call — and, crucially, **without** concurrent turns on
the shared kernel instance racing on instance fields.

The turn is also the home of cross-cutting concerns:

- ``span`` — the turn's observability span (:class:`~agent.telemetry.TurnSpan`).
- ``cancel_token`` — cooperative cancellation checked at step boundaries (task
  cancellation additionally propagates ``CancelledError`` into whatever the loop awaits).
- ``usage`` / ``step_usage`` — per-step token & cost accounting.
- ``progress_sink`` — an optional callable receiving structured events (plan output, tool
  progress, approval requests) for streaming to the client.
- ``loop_tracker`` — tool-oscillation detection (see :class:`ToolLoopTracker`).
- ``max_budget_usd`` — hard per-turn cost cap; the loop aborts when exceeded.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

# The span type is imported lazily to avoid a module cycle (telemetry reads contextvar-only).
TurnSpan = Any

# Modules that have not created an :class:`AgentTurn` (tests, direct tool calls) read
# ``current_turn()`` as ``None`` and degrade gracefully.
_TURN_CTX: ContextVar[AgentTurn | None] = ContextVar("agent_turn", default=None)


def current_turn() -> AgentTurn | None:
    """The :class:`AgentTurn` bound to the current task, or ``None`` outside a turn."""
    return _TURN_CTX.get()


def bind_turn(turn: AgentTurn | None) -> None:
    """Bind ``turn`` to the current task (no-op with ``None``). Call once per turn."""
    if turn is not None:
        _TURN_CTX.set(turn)


@dataclass
class AgentTurn:
    """Everything a single agent turn needs, scoped per-run (never shared across turns)."""

    user_msg: str
    history: list[dict] | None = None
    session_memory: Any | None = None
    memory_keys: list[str] | None = None
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    turn_id: str = field(default_factory=lambda: str(uuid4()))

    # ── per-turn mutable state (moved off the singletons) ──
    recall_hits: list | None = None          # proactive memory recall, computed once per turn
    injected: list[tuple[str, str]] = field(default_factory=list)  # agent.inject() content
    memory_brief: str = ""                   # MEMORY.md session brief, loaded once per turn

    # ── accounting ──
    usage: dict[str, int] = field(
        default_factory=lambda: {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    )
    step_usage: list[dict] = field(default_factory=list)

    # ── reliability / control ──
    cancel_token: asyncio.Event = field(default_factory=asyncio.Event)
    max_budget_usd: float | None = None
    loop_tracker: Any | None = None           # ToolLoopTracker (see agent.loop_guard)

    # ── observability / streaming ──
    span: TurnSpan | None = None
    audit: Any | None = None                # AuditSink (agent.telemetry); JSONL turn trail
    progress_sink: Callable[[dict], None] | None = None
    checkpoint_id: str | None = None         # pre-turn workspace snapshot (agent.checkpoints)

    # ── approval (human-in-the-loop) ──
    approvals: Any | None = None              # ApprovalStore (see agent.approvals)

    def inject(self, text: str, *, name: str | None = None) -> None:
        """Append durable dynamic content to the prompt suffix for this turn."""
        self.injected.append((name or f"inject:{len(self.injected)}", text))

    def emit(self, event: dict) -> None:
        """Stream a structured event to the client (plan / tool progress / approvals)."""
        if self.progress_sink is not None:
            self.progress_sink(event)

    def add_usage(self, step: dict | None) -> None:
        """Accumulate one step's token counts into ``usage``; record the step breakdown."""
        if step:
            self.step_usage.append(dict(step))
            for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                self.usage[key] = self.usage.get(key, 0) + int(step.get(key) or 0)
