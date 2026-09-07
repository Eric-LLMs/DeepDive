"""Port contracts: the seams where an adapter plugs the core into its own world.

Every protocol here is a *capability*, never a domain object: the core asks, the adapter
answers in neutral types (mappings, strings, :class:`LeaseLedger`). Physical execution
details — which model, which endpoint, which worker — are runtime facts that ride in
``TaskRequest.hints`` and belong to NO definition spec (calibration #3).
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from workflow.leases import LeaseLedger
from workflow.policy import Grade


# ── lease/state storage ───────────────────────────────────────────────────────
@runtime_checkable
class LeaseStore(Protocol):
    """An atomic read-modify-write section over the run's lease ledger.

    ``atomic`` MUST run ``mutate`` and persist its result under the adapter's own
    compare-and-set (file lock + revision, DB transaction, …); concurrent callers are
    serialized. ``read`` is a plain authoritative snapshot. ``cancel_requested`` flips
    are written by external agents (a UI stop button) through the same ledger.
    """

    def atomic(self, mutate: Callable[[LeaseLedger], LeaseLedger]) -> LeaseLedger: ...

    def read(self) -> LeaseLedger: ...


# ── task execution (the black-box boundary) ──────────────────────────────────
@dataclass(frozen=True)
class TaskRequest:
    """What the core hands an executor. ``prompt`` is opaque to the core — the adapter
    composes it; the core never interprets its content."""

    prompt: str
    hints: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TaskResult:
    """Executor output. ``spend=None`` means UNKNOWN, distinct from a real ``0.0``."""

    value: str
    spend: float | None = 0.0


@runtime_checkable
class Executor(Protocol):
    """The one place non-determinism lives: input -> execute -> result.

    The workflow never learns (or needs) how the executor reasons internally.
    """

    async def execute(self, request: TaskRequest) -> TaskResult: ...


# ── continuation scheduling ──────────────────────────────────────────────────
@runtime_checkable
class Scheduler(Protocol):
    """Hand off the next iteration. MUST raise on delivery failure — the runner then
    terminalizes the execution honestly instead of stranding the slot (no orphan leases).
    """

    async def schedule_next(self, request: Mapping[str, Any]) -> None: ...


# ── wake-up hints (advisory, never load-bearing) ─────────────────────────────
@runtime_checkable
class EventPublisher(Protocol):
    async def publish(self, *, kind: str, revision: int | None = None) -> None: ...


class NoopPublisher:
    """Default publisher: lifecycle hints are an optimization, absence is legal."""

    async def publish(self, *, kind: str, revision: int | None = None) -> None:
        return None


# ── terminal-override hook ───────────────────────────────────────────────────
@runtime_checkable
class TerminalHook(Protocol):
    """Last look before a terminal grade is executed (the generic fallback/compensation
    seam — SFN Catch, Conductor failure hooks). Return a replacement Grade to rewrite
    the stop (including to ``None`` = continue), or ``None`` to keep the outcome.
    """

    async def before_terminalize(
        self, grade: Grade, facts: Mapping[str, Any]
    ) -> Grade | None: ...


# ── runtime knobs bundle ─────────────────────────────────────────────────────
@dataclass(frozen=True)
class RuntimeContext:
    """Per-run runtime facts, injected at drive time (NEVER part of a definition spec)."""

    values: Mapping[str, Any] = field(default_factory=dict)
