"""Phase 3 ViewerExecutor: grounded single-shot over already-injected blocks.

Pins the two things that make this branch safe and SSE-identical to DIRECT:

* the system prompt is the grounded preamble PLUS the exact reference text the shared
  ``render_reference`` (``viewer_context.render_viewer_reference``) produces from the
  assembly's blocks — so the viewer fast path and the Agent path present the SAME
  [Vn] content for the same injected selection (constraint #3: reuse, not re-implement);
* the whole stream/run machinery is inherited unchanged — one content→done shape,
  ``tools=None`` (a grounded turn never dispatches read_document/vision), and the user
  + assistant rows still land in session memory (constraints #5/#7: no coarse read, no
  touching the Agent/RAG/Memory code).
"""
from __future__ import annotations

from types import SimpleNamespace

from core.application.chat.execution_plan import ExecutionPlan, PlanKind
from core.application.chat.executors.base import TurnRequest
from core.application.chat.executors.direct import DirectExecutor
from core.application.chat.executors.viewer import _VIEWER_PREAMBLE, ViewerExecutor


class FakeLLM:
    def __init__(self, deltas=("It says", " hello"), usage=None):
        self._deltas = list(deltas)
        self._usage = usage if usage is not None else {"total_tokens": 7}
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


class FakeRenderer:
    """Stands in for ``viewer_context.render_viewer_reference`` — records the blocks it
    is handed and returns a deterministic [Vn] section."""

    def __init__(self, out="[V1] selected text"):
        self._out = out
        self.seen: list = []

    def __call__(self, blocks):
        self.seen.append(blocks)
        return self._out


def _block(kind="selection", img=None):
    return SimpleNamespace(kind=kind, image_asset_id=img, text="selected text", label="p.1")


def _req(*, blocks=None, status="injected", renderer_out="[V1] selected text", message="what does this say"):
    assembly = {"status": status, "blocks": blocks if blocks is not None else [_block()]}
    ctx = SimpleNamespace(
        body=SimpleNamespace(message=message, attach=None), user_text=message,
        history=[], model="m", base_url=None, api_key=None, disable_thinking=False,
        session_memory=FakeMemory(), viewer_assembly=assembly,
        research_turn=False, effective_handoff=None,
    )
    renderer = FakeRenderer(renderer_out)
    viewer = SimpleNamespace(render_reference=renderer)
    deps = SimpleNamespace(
        agent=SimpleNamespace(loop=SimpleNamespace(llm=FakeLLM())), viewer=viewer,
    )
    req = TurnRequest(ctx=ctx, deps=deps, plan=ExecutionPlan(kind=PlanKind.VIEWER))
    req.renderer = renderer  # type: ignore[attr-defined]  # test handle for assertions
    return req


def test_kind_is_viewer():
    assert ViewerExecutor.kind is PlanKind.VIEWER
    # It reuses the entire DirectExecutor machinery — only the prompt is overridden.
    assert issubclass(ViewerExecutor, DirectExecutor)


def test_system_prompt_is_grounded_on_rendered_blocks():
    req = _req()
    prompt = ViewerExecutor().system_prompt(req)
    assert prompt.startswith(_VIEWER_PREAMBLE)
    # The SHARED renderer ran on the assembly's blocks (reuse, not re-implementation).
    assert req.renderer.seen[0] == req.ctx.viewer_assembly["blocks"]
    assert "[V1] selected text" in prompt


def test_system_prompt_without_rendered_text_is_bare_preamble():
    req = _req(renderer_out="")
    assert ViewerExecutor().system_prompt(req) == _VIEWER_PREAMBLE


async def test_stream_builds_grounded_request_and_keeps_direct_shape():
    req = _req()
    events = [e async for e in ViewerExecutor().stream(req, progress_sink=lambda e: None)]
    kinds = [e["type"] for e in events]
    assert kinds == ["content", "content", "done"]
    assert events[-1]["data"]["answer"] == "It says hello"
    assert events[-1]["data"]["usage"] == {"total_tokens": 7}
    assert events[-1]["data"]["error"] is None
    # A grounded turn never advertises tools (read_document/vision stay on the Agent).
    assert req.deps.agent.loop.llm.calls[0]["tools"] is None
    # The system message carries the rendered [Vn] section; user text rides as the last turn.
    sent = req.deps.agent.loop.llm.calls[0]["request"]
    assert sent[0]["role"] == "system" and "[V1] selected text" in sent[0]["content"]
    assert sent[-1] == {"role": "user", "content": "what does this say"}
    # user + assistant persisted, exactly like DIRECT.
    assert req.ctx.session_memory.appended == [("user", "what does this say"), ("assistant", "It says hello")]


async def test_run_returns_directresult_shape_grounded():
    req = _req()
    result = await ViewerExecutor().run(req)
    assert result.final_answer == "It says hello"
    assert result.usage == {"total_tokens": 7}
    assert result.error is None
    assert result.messages[-1]["role"] == "assistant"
    sent = req.deps.agent.loop.llm.calls[0]["request"]
    assert "[V1] selected text" in sent[0]["content"]
