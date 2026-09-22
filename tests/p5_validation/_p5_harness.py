"""Phase 5 post-implementation validation harness — REAL production assembly.

Everything the user sees rides the real code path end to end:

    httpx → /chat/stream (apps/api/routers/chat.py) → TurnOrchestrator
      → executors (direct / retrieval / viewer / action / composite / agent)
      → REAL AgentKernel + ReactLoopAgent + ToolRuntime + Sandbox + ApprovalStore
      → REAL registered tools (create_folder / add_term / web_search)

Only the OUTER world is faked, deterministically: the LLM port (ScriptedPort),
the retrieval seam, the cloud-drive / vocabulary / web-search services behind the
tools, and the DB (via the shared memory-layer fakes). No real traffic, no real
side effects, no network — this is a local simulation of the gray release.

The critical difference from the Phase 4/5 golden tests is that the ACTION branch
is NOT monkeypatched here: ``chat._run_tool`` is left REAL, so a direct dispatch
traverses the SAME sandbox ASK → approval → guard → tool-body funnel the Agent
takes. That is what lets a test assert fast-path-vs-legacy parity on permission
decisions and side-effect counts, not just on the answer text.

Helpers exported:
  * ``ScriptedPort``      — the reliability-wrapped LLM double (agent steps vs
    single-shot generation vs the CRAG judge, discriminated by call shape).
  * ``Spy`` + ``make_drive`` / ``make_vocab`` / ``make_web`` — service doubles that
    RECORD side effects and inject the preflight / post-write failure modes.
  * ``AutoApprovalBroker`` — resolves the ASK future allow/deny/timeout.
  * ``build_kernel`` / ``build_app`` / ``sse``   — real-assembly fixtures.
  * ``RoutingCtx`` / ``FakeBody`` + ``plan_for`` — the pure Layer-A routing probe
    (resolve_requirements + build_execution_plan with no I/O at all).
"""
from __future__ import annotations

import asyncio
import json
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
from agent import Context, SkillRegistry, ToolRuntime
from agent.engine.kernel import AgentKernel
from agent.llm.llm_errors import LLMFatalError
from agent.security.approvals import ApprovalBridge, MemoryApprovalBroker
from agent.security.sandbox import Sandbox, SandboxRule
from api.auth import AuthUser, require_user_optional
from api.deps import get_drive_service, get_task_queue
from api.routers import chat as chat_mod
from api.routers.chat import router as chat_router
from api.tools import add_term_tool as add_term_mod
from api.tools import create_folder_tool as create_folder_mod
from api.tools import web_search_tool as web_search_mod
from core.application.chat.execution_plan import PolicyContext as PPolicyContext
from core.application.chat.execution_plan import build_execution_plan
from core.application.chat.understanding import resolve_requirements
from core.application.drive_service import DriveError
from core.config import settings
from core.infrastructure.db import UserRoleModel
from core.infrastructure.request_context import set_request_user
from fastapi import FastAPI

from tests._memory_v2_fakes import Db, FakeSession, Llm

USER = uuid4()

# Discriminates the CRAG judge (a ``chat`` call whose system prompt is rag's) from a
# single-shot non-streaming generation (DIRECT/RAG ``chat`` with tools=None). The
# string is the literal head of ``rag.nodes.crg_check.judge_relevance``'s system prompt.
_JUDGE_SYS_PREFIX = "You judge whether retrieved evidence"


# ── LLM double ───────────────────────────────────────────────────────────────────
class ScriptedPort:
    """Deterministic stand-in for the model, reached through the kernel's ReliableLLM.

    Three call shapes to tell apart, exactly as production does:

      * ``chat_stream(tools=None)``      — a single-shot, tool-less generation (the
        DIRECT / VIEWER / RAG / COMPOSITE grounded answers).
      * ``chat_stream(tools=[...])``     — one AGENT-ReAct step: it may emit content
        deltas and/or a ``tool_calls`` event, then usage. Steps are consumed from a
        queue so a multi-step (tool → answer) legacy turn replays deterministically.
      * ``chat``                         — the CRAG judge (verdict) when the system
        prompt is rag's, otherwise a non-streaming agent step / single-shot answer.

    Counters let a test prove which channel a turn used: a certified ACTION must
    drive ``single_shot == steps == judged == 0`` (no LLM anywhere); a fail-closed
    RAG escalation pays ``judged == 1`` but ``single_shot == 0`` (the Agent answers).
    """

    def __init__(
        self,
        *,
        steps=None,
        deltas=None,
        text=None,
        verdict="relevant",
        fail=None,
    ) -> None:
        # ``steps``: list of {"thinking":[...], "content":[...], "tool_calls":[...]|None}
        self._steps = deque(steps or [])
        self._deltas = list(deltas) if deltas is not None else ["Direct answer."]
        self._text = text if text is not None else "".join(self._deltas)
        self._verdict = verdict
        self.fail = fail  # None | "stream" | "chat" → raise LLMFatalError on that shape
        self.single_shot = 0
        self.steps = 0
        self.judged = 0
        self.requests: list[list[dict]] = []  # every prompt the port saw (injection probes)

    @staticmethod
    def _usage() -> dict:
        return {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}

    def _is_judge(self, messages) -> bool:
        return bool(messages) and str(messages[0].get("content", "")).startswith(_JUDGE_SYS_PREFIX)

    def _next_step(self) -> dict:
        if self._steps:
            return self._steps.popleft()
        return {"content": ["Done."], "tool_calls": None}  # terminal: loop breaks

    @staticmethod
    def _norm_tc(x: dict) -> dict:
        args = x.get("arguments", {})
        return {
            "id": x.get("id") or str(uuid4()),
            "name": x["name"],
            "arguments": args if isinstance(args, str) else json.dumps(args, ensure_ascii=False),
        }

    async def chat(self, messages, *, tools=None, model=None, base_url=None, api_key=None):
        self.requests.append([dict(m) for m in messages])
        if self._is_judge(messages):
            self.judged += 1
            return {"content": json.dumps({"verdict": self._verdict}), "tool_calls": [], "usage": self._usage()}
        if tools is not None:
            step = self._next_step()
            self.steps += 1
            return {
                "content": "".join(step.get("content") or []),
                "tool_calls": [self._norm_tc(t) for t in (step.get("tool_calls") or [])],
                "usage": self._usage(),
            }
        if self.fail == "chat":
            raise LLMFatalError("provider down")
        self.single_shot += 1
        return {"content": self._text, "tool_calls": [], "usage": self._usage()}

    async def chat_stream(
        self, messages, *, tools=None, model=None, base_url=None, api_key=None,
        disable_thinking=False,
    ):
        self.requests.append([dict(m) for m in messages])
        if tools is None:
            if self.fail == "stream":
                raise LLMFatalError("provider down")
            self.single_shot += 1
            for d in self._deltas:
                if d:
                    yield {"type": "content", "data": d}
            yield {"type": "usage", "data": self._usage()}
            return
        step = self._next_step()
        self.steps += 1
        for d in step.get("thinking") or []:
            if d:
                yield {"type": "thinking", "data": d}
        for d in step.get("content") or []:
            if d:
                yield {"type": "content", "data": d}
        if step.get("tool_calls"):
            yield {"type": "tool_calls", "data": [self._norm_tc(t) for t in step["tool_calls"]]}
        yield {"type": "usage", "data": self._usage()}


# ── effect recorder + service doubles ──────────────────────────────────────────────
@dataclass
class Spy:
    folders_created: list = field(default_factory=list)   # (user_id, name)
    terms_added: list = field(default_factory=list)       # (domain_id, word, user_id, definition)
    web_queries: list = field(default_factory=list)       # query strings


def make_drive(spy: Spy, *, mode: str = "ok"):
    """DriveService double. ``mode`` selects the side-effect boundary outcome:

      * "ok"          — write happens, deterministic confirmation;
      * "preflight"   — DriveError BEFORE any write ⇒ body re-raises as "preflight:"
                        ⇒ ActionPreflightFailure ⇒ escalate (no side effect recorded);
      * "post-write"  — write recorded, THEN a mid-write failure ⇒ STATE UNKNOWN
                        (never a blind Agent retry).
    """

    class _Drive:
        def __init__(self, session_factory):
            pass

        async def create_folder(self, user_id, workspace_id, parent_path, name):
            if mode == "preflight":
                raise DriveError("no access to folder", 403)
            spy.folders_created.append((str(user_id), name))
            if mode == "post-write":
                raise RuntimeError("connection reset mid-write")
            return {"path": name, "id": "f1"}

        async def ensure_asset_readable(self, user_id, asset_id):
            return None

    return _Drive


def make_vocab(spy: Spy, domains):
    """VocabularyService double over an explicit visible-domain list (constraint 3)."""

    class _FakeVocab:
        last = None

        def __init__(self, *_a, **_k):
            self.added_here = 0
            type(self).last = self

        async def list_domains(self, user_id):
            return list(domains)

        async def add_term(self, domain_id, word, definition="", user_id=None):
            spy.terms_added.append((domain_id, word, str(user_id), definition))
            self.added_here += 1
            return SimpleNamespace(word=word)

    return _FakeVocab


def make_web(spy: Spy, *, status: str = "ok", results=None):
    """web_search provider seam (sync ``search`` — the tool runs it in a thread)."""

    class _Provider:
        def search(self, query, top_k=5):
            spy.web_queries.append(query)
            if status == "degraded":
                return {
                    "status": "degraded", "provider": "fake",
                    "error": {"type": "timeout", "message": "engine down"},
                }
            return {"status": "ok", "results": results or [{"title": "t", "url": "u", "snippet": "s"}]}

    return _Provider()


def domains_named(*names):
    return [SimpleNamespace(id=f"d{i}", name=n) for i, n in enumerate(names)]


class AutoApprovalBroker(MemoryApprovalBroker):
    """Resolves the ASK future deterministically: allow / deny / timeout.

    ``timeout`` leaves the future pending so ``ApprovalStore.request``'s
    ``wait_for`` expires (build_app lowers ``approval_timeout_seconds``); the
    ``requests`` list records that an approval was actually surfaced (the ASK frame
    reaches the SSE sink regardless of mode).
    """

    def __init__(self, mode: str = "allow") -> None:
        super().__init__()
        self.mode = mode
        self.requests: list[str] = []

    async def register(self, approval_id, future, *, user_id=None):
        await super().register(approval_id, future, user_id=user_id)
        self.requests.append(approval_id)
        if self.mode == "allow":
            future.set_result((True, None))
        elif self.mode == "deny":
            future.set_result((False, "the user declined the operation"))


class _HttpSession(FakeSession):
    async def get(self, model, pk):
        return None


class _SessionCtx:
    async def __aenter__(self):
        return "session"

    async def __aexit__(self, *a):
        return False


class _DriveReader:
    async def ensure_asset_readable(self, user_id, asset_id):
        return None


class FakeSeam:
    """Cache-wrapped retrieval seam double (retrieve() contract identical to the tool)."""

    def __init__(self, hits=None, *, raise_exc=False):
        self.hits = list(hits or [])
        self.raise_exc = raise_exc
        self.calls: list[tuple] = []

    async def retrieve(self, query, top_k=5, filters=None):
        self.calls.append((query, top_k, filters))
        if self.raise_exc:
            raise RuntimeError("retrieval backend unavailable")
        return self.hits


# ── real-kernel + app assembly ─────────────────────────────────────────────────────
def build_kernel(
    monkeypatch, port: ScriptedPort, spy: Spy, *,
    broker_mode: str = "allow", drive_mode: str = "ok", domains=None,
    web_status: str = "ok", grant=(), rules=(), audit_dir: str = ".",
):
    """Compose the REAL AgentKernel with the REAL seed tools registered.

    Returns ``(kernel, runtime, ctx, broker)``. ``get_agent`` is NOT yet patched —
    that happens in :func:`build_app`, which must reference the same kernel so the
    real ``_run_tool`` seam and the AgentExecutor share one runtime/sandbox funnel.
    """
    broker = AutoApprovalBroker(broker_mode)
    runtime = ToolRuntime(approval=ApprovalBridge(broker))
    ctx = Context()
    ctx.provide("session_factory", lambda: _SessionCtx())
    ctx.provide("web_search", make_web(spy, status=web_status))
    ctx.provide("retrieval", None)

    monkeypatch.setattr(create_folder_mod, "DriveService", make_drive(spy, mode=drive_mode))
    if domains is not None:
        monkeypatch.setattr(add_term_mod, "VocabularyService", make_vocab(spy, domains))

    create_folder_mod.register(runtime, ctx, None)
    add_term_mod.register(runtime, ctx, None)
    web_search_mod.register(runtime, ctx, None)

    # Keep the audit trail off the repo; AuditSink falls back to structlog when the
    # dir does not exist, so point it at a scratch path under the tests dir.
    monkeypatch.setattr(settings, "audit_log_path", Path(audit_dir) / "_p5_audit.jsonl", raising=False)

    sandbox = Sandbox()
    for p in grant:
        sandbox.grant(p)
    for perm, decision in rules:
        sandbox.add_rule(SandboxRule(perm, decision))

    kernel = AgentKernel(
        port, runtime, soul="You are Delveta (validation).",
        skills=SkillRegistry(), sandbox=sandbox, memory=None, checkpoints=None,
    )
    return kernel, runtime, ctx, broker


def _gates(
    monkeypatch, *, fast=True, direct=False, viewer=False, retrieval=False,
    action=True, composite=False,
):
    monkeypatch.setattr(settings, "chat_fast_paths_enabled", fast, raising=False)
    monkeypatch.setattr(settings, "chat_direct_fast_path_enabled", direct, raising=False)
    monkeypatch.setattr(settings, "chat_viewer_fast_path_enabled", viewer, raising=False)
    monkeypatch.setattr(settings, "chat_retrieval_fast_path_enabled", retrieval, raising=False)
    monkeypatch.setattr(settings, "chat_action_fast_path_enabled", action, raising=False)
    monkeypatch.setattr(settings, "chat_composite_fast_path_enabled", composite, raising=False)


def build_app(monkeypatch, port, seam, kernel, broker, *, user=USER):
    """Wire the real /chat/stream router onto fake seams — keeping ``_run_tool`` REAL."""
    async def _route(session, token, role_id):
        return "http://fake", "key", "model", "biz", None

    async def _authz(session, uid, role):
        return "free"

    async def _persist(message_id, key, value):
        return None

    async def _usage(*a, **k):
        return None

    monkeypatch.setattr(chat_mod, "SessionLocal", lambda: _HttpSession(Db(rows=[])))
    monkeypatch.setattr(chat_mod, "llm", Llm())
    monkeypatch.setattr(chat_mod, "_embedder", lambda: None)
    monkeypatch.setattr(chat_mod, "get_retriever", lambda: seam)
    monkeypatch.setattr(chat_mod, "_resolve_chat_route", _route)
    monkeypatch.setattr(chat_mod, "authorize_usage", _authz)
    monkeypatch.setattr(chat_mod, "_resolve_research_context", lambda *a, **k: (None, None, None, None))
    monkeypatch.setattr(chat_mod, "_persist_turn_meta", _persist)
    monkeypatch.setattr(chat_mod, "_log_usage", _usage)
    monkeypatch.setattr(chat_mod, "get_approval_bridge", lambda: SimpleNamespace(broker=broker))
    # The seam-critical line: get_agent returns the REAL kernel, so _run_tool executes
    # through the SAME runtime/sandbox/approval funnel the Agent would.
    monkeypatch.setattr(chat_mod, "get_agent", lambda: kernel)

    if broker.mode == "timeout":
        monkeypatch.setattr(settings, "approval_timeout_seconds", 0.05, raising=False)

    app = FastAPI()
    app.include_router(chat_router)
    app.dependency_overrides[require_user_optional] = lambda: AuthUser(
        user_id=user, username="alice", display_name=None,
        role=UserRoleModel(role_id="user", role_name="User"), token_id=uuid4())
    app.dependency_overrides[get_task_queue] = lambda: SimpleNamespace(
        enqueue=lambda *a, **k: asyncio.sleep(0))
    app.dependency_overrides[get_drive_service] = lambda: _DriveReader()
    return app


@dataclass
class TurnResult:
    events: list[dict]
    types: list[str]
    done: dict | None
    approvals: list[dict]
    answer: str | None
    wall_ms: float

    @property
    def content_text(self) -> str:
        return "".join(e["data"] for e in self.events if e.get("type") == "content")


async def sse(app, message, *, user=USER, **fields) -> TurnResult:
    """POST /chat/stream, parse every SSE frame, time the whole turn (TTFT proxy)."""
    set_request_user(user)
    body = {"message": message, "session_id": str(uuid4()), **fields}
    transport = httpx.ASGITransport(app=app)
    events: list[dict] = []
    done = None
    t0 = asyncio.get_running_loop().time()
    async with httpx.AsyncClient(transport=transport, base_url="http://t", timeout=30) as client, \
            client.stream("POST", "/chat/stream", json=body) as r:
        assert r.status_code == 200
        async for line in r.aiter_lines():
            if not line.startswith("data:"):
                continue
            evt = json.loads(line[5:].strip())
            events.append(evt)
            if evt.get("type") == "done":
                done = evt.get("data")
    wall_ms = (asyncio.get_running_loop().time() - t0) * 1000
    return TurnResult(
        events=events, types=[e.get("type") for e in events], done=done,
        approvals=[e["data"] for e in events if e.get("type") == "approval-request"],
        answer=(done or {}).get("answer") if done else None, wall_ms=wall_ms,
    )


# ── pure Layer-A routing probe (no I/O) ─────────────────────────────────────────────
@dataclass
class FakeBody:
    message: str = ""
    attach: dict | None = None
    handoff: dict | None = None
    viewer: dict | None = None
    context_state: object | None = None
    tail: list = field(default_factory=list)
    disable_thinking: bool = False
    ephemeral: bool = False
    guest_token: str | None = None
    session_id: str | None = None


@dataclass
class RoutingCtx:
    """The handful of ChatTurnContext fields ``resolve_requirements`` reads."""

    body: FakeBody
    viewer_assembly: dict | None = None
    research_turn: bool = False
    effective_handoff: dict | None = None
    owned_asset_id: str | None = None


def plan_for(ctx: RoutingCtx, message: str, *, fast=True, direct=False, viewer=False,
             retrieval=False, action=True, composite=False):
    """L0 (pure) → policy (pure): the resolved requirement set and execution plan."""
    reqs = resolve_requirements(ctx, message)
    policy = PPolicyContext(
        fast_paths_enabled=fast,
        direct_fast_path_enabled=direct,
        viewer_fast_path_enabled=viewer,
        retrieval_fast_path_enabled=retrieval,
        action_enabled=action,
        composite_enabled=composite,
    )
    return reqs, build_execution_plan(reqs, policy)


def orchestrator_kind(ctx: RoutingCtx, message: str, **gates) -> str:
    """The PlanKind the LIVE control plane would pick (gates via settings, no HTTP)."""
    # Convenience wrapper so Layer-A can assert through the same entry point the
    # orchestrator uses; resolve_plan also sinks source_policy onto ctx when live.
    _, plan = plan_for(ctx, message, **gates)
    return plan.kind.value
