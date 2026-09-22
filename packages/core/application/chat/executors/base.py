"""Executor contracts: what the orchestrator hands a capability, and what comes back.

``ChatEvent`` is the same ``{"type": ..., "data": ...}`` dict the SSE transport has
always emitted (agent events, approvals, viewer short-circuit, done) — executors
produce events, the orchestrator forwards them VERBATIM so the frontend contract
(web + desktop) never changes.
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from core.application.chat.execution_plan import ExecutionPlan, PlanKind

if TYPE_CHECKING:  # avoid an import cycle: context.py TYPE_CHECKING-imports base
    from core.application.chat.context import ChatTurnContext

# The wire payload of one SSE data frame — deliberately a plain dict (legacy shape).
ChatEvent = dict[str, Any]

ProgressSink = Callable[[ChatEvent], None]


@dataclass(frozen=True)
class ViewerDeps:
    """The pure assembly functions from ``apps/api/viewer_context.py`` (injected so
    this package never imports the api layer)."""

    build_blocks: Callable[..., dict]
    validate_citations: Callable[..., tuple[list, list]]
    citation_map: Callable[..., dict]
    snapshot: Callable[..., dict]
    # Renders the injected reference blocks into the grounded-prompt section (the
    # viewer fast path reuses the SAME renderer the Agent path's DYNAMIC_SUFFIX uses).
    render_reference: Callable[..., str] = lambda blocks: ""


@dataclass
class ChatDeps:
    """Everything the control plane needs from the host process, resolved per request.

    The router wires these from its own module globals so existing test seams
    (monkeypatched ``get_agent`` / ``_log_usage`` / ``SessionLocal`` / …) keep
    working unchanged.
    """

    session_factory: Any
    queue: Any                                   # core.infrastructure.jobs.TaskQueue
    drive: Any                                   # core.application.drive_service.DriveService
    agent: Any                                   # AgentKernel instance
    llm: Any
    embedder: Callable[[], Any]
    viewer: ViewerDeps
    new_approval_bridge: Callable[[], Any]
    persist_turn_meta: Callable[[str | None, str, Any], Awaitable[None]]
    log_usage: Callable[..., Awaitable[None]]
    resolve_research: Callable[..., tuple]
    # The cache-wrapped retrieval seam (RAGPipeline or gRPC client) — the SAME object
    # the agent Context provides as "retrieval", so the fast path inherits the tool's
    # ACL / tenant / query-cache semantics for free (Phase 4). None = not wired →
    # the LOCAL_RAG branch degrades to the Agent before any retrieval is attempted.
    retriever: Any = None


class EscalateToAgent(Exception):
    """Pre-commit fallback signal (design §5 Commit Point): a fast-path executor raises
    this BEFORE emitting any user-visible event to hand the turn back to the Agent.
    After the first content delta the channel is locked and this must never be raised.

    Phase 4 uses it for the fail-closed RAG contract: a private retrieval failure or an
    insufficient/empty result set escalates to the Agent — it NEVER re-routes to a
    public-web path, and it never answers over missing evidence.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class TurnRequest:
    """One executor invocation: resolved context + the plan branch it must serve."""

    ctx: ChatTurnContext
    deps: ChatDeps
    plan: ExecutionPlan


class ChatExecutor(Protocol):
    """A capability branch. ``stream`` yields ChatEvents; ``run`` is the non-stream
    equivalent returning an AgentResult-shaped object.

    Executors must not emit the final ``done`` event — the orchestrator assembles it
    in :mod:`core.application.chat.lifecycle`.
    """

    kind: PlanKind

    def stream(self, req: TurnRequest, *, progress_sink: ProgressSink) -> AsyncIterator[ChatEvent]: ...

    async def run(self, req: TurnRequest) -> Any: ...
