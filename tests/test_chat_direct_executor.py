"""Phase 2 DirectExecutor: single-shot shape + SSE contract + persistence.

Pins that the DIRECT branch (behind the master + DIRECT gates) produces the same
``content`` / terminal ``done`` event shape the transport already speaks, emits no
tool activity, and still appends the user + assistant rows to session memory so the
transcript / usage bookkeeping is uniform with the Agent path.
"""
from __future__ import annotations

from types import SimpleNamespace

from core.application.chat.execution_plan import ExecutionPlan, PlanKind
from core.application.chat.executors.base import TurnRequest
from core.application.chat.executors.direct import DirectExecutor


class FakeLLM:
    """Stands in for the kernel's reliability-wrapped port (``agent.loop.llm``)."""

    def __init__(self, deltas=("Hello", " there"), usage=None):
        self._deltas = list(deltas)
        self._usage = usage if usage is not None else {"total_tokens": 5}
        self.calls: list[dict] = []

    async def chat_stream(self, request, *, tools=None, model=None, base_url=None,
                          api_key=None, disable_thinking=False):
        self.calls.append({"request": request, "tools": tools})
        for d in self._deltas:
            yield {"type": "content", "data": d}
        yield {"type": "usage", "data": self._usage}

    async def chat(self, request, *, tools=None, model=None, base_url=None, api_key=None):
        self.calls.append({"request": request, "tools": tools})
        return {"content": "".join(self._deltas), "tool_calls": [], "usage": self._usage}


class FakeMemory:
    def __init__(self):
        self.appended: list[tuple[str, str]] = []

    async def append_message(self, role, text):
        self.appended.append((role, text))


def _req(message="hi"):
    ctx = SimpleNamespace(
        body=SimpleNamespace(message=message, attach=None), user_text=message,
        history=[{"role": "user", "content": "prior"}], model="m", base_url=None,
        api_key=None, disable_thinking=False, session_memory=FakeMemory(),
        viewer_assembly=None, research_turn=False, effective_handoff=None,
    )
    deps = SimpleNamespace(agent=SimpleNamespace(loop=SimpleNamespace(llm=FakeLLM())))
    return TurnRequest(ctx=ctx, deps=deps, plan=ExecutionPlan(kind=PlanKind.DIRECT))


async def test_stream_yields_content_then_done_and_persists():
    req = _req()
    req.deps.agent.loop.llm = FakeLLM(deltas=("Hel", "lo"))
    events = [e async for e in DirectExecutor().stream(req, progress_sink=lambda e: None)]
    kinds = [e["type"] for e in events]
    assert kinds == ["content", "content", "done"]
    assert events[-1]["data"]["answer"] == "Hello"
    assert events[-1]["data"]["usage"] == {"total_tokens": 5}
    assert events[-1]["data"]["error"] is None
    # user + assistant appended in order; assistant carries the joined answer.
    assert req.ctx.session_memory.appended == [("user", "hi"), ("assistant", "Hello")]
    # tools must be None (a direct turn never dispatches a tool).
    assert req.deps.agent.loop.llm.calls[0]["tools"] is None


async def test_stream_error_shape():
    req = _req()
    from agent.llm.llm_errors import LLMFatalError

    class Boom(FakeLLM):
        async def chat_stream(self, request, **kw):
            yield {"type": "content", "data": "par"}
            raise LLMFatalError("upstream 500")

    req.deps.agent.loop.llm = Boom()
    events = [e async for e in DirectExecutor().stream(req, progress_sink=lambda e: None)]
    types = [e["type"] for e in events]
    assert types == ["content", "error", "done"]
    assert events[-1]["data"]["error"] == "upstream 500"
    # No assistant row is persisted for a failed turn.
    assert ("assistant", "par") not in req.ctx.session_memory.appended


async def test_run_returns_agentresult_shape():
    req = _req()
    result = await DirectExecutor().run(req)
    assert result.final_answer == "Hello there"
    assert result.usage == {"total_tokens": 5}
    assert result.error is None
    assert result.messages[-1]["role"] == "assistant"
    assert req.ctx.session_memory.appended == [("user", "hi"), ("assistant", "Hello there")]
