"""Phase 5B CompositeExecutor: fan-out → shared judge → ONE grounded generation.

Pins constraint 4 (cost transparency): the Sufficiency Judge is a separate ``chat``
(evaluation) call and the final answer is exactly one ``chat_stream`` (generation) —
FakeLLM counts the two channels independently, so a regression that folded the judge
into the generation (or generated twice) fails here, not in production.

Fail-closed semantics are inherited from Phase 4 unchanged: seam missing / retrieval
failure / empty recall / non-``relevant`` verdict ⇒ EscalateToAgent BEFORE any event
(no user row even persisted — the Agent replays clean). The grounding carries BOTH
evidence classes as fenced DATA ([Vn] viewer blocks + [Rn] retrieved passages).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from core.application.chat.execution_plan import ExecutionPlan, PlanKind
from core.application.chat.executors.base import EscalateToAgent, TurnRequest
from core.application.chat.executors.composite import CompositeExecutor

from tests.test_chat_retrieval_executor import (
    FakeLLM,
    FakeMemory,
    FakeRetriever,
)

VIEWER_TEXT = "V1 (report.pdf p.3): The mitochondria is the powerhouse of the cell."
MESSAGE = "结合这一页的内容，我的知识库里还有关于线粒体的哪些资料"


class _ViewerDeps:
    def __init__(self, text=VIEWER_TEXT):
        self.text = text
        self.calls: list = []

    def render_reference(self, blocks):
        self.calls.append(list(blocks))
        return self.text


def _req(*, retriever=..., verdict="relevant", viewer_text=VIEWER_TEXT):
    ctx = SimpleNamespace(
        body=SimpleNamespace(message=MESSAGE, attach=None), user_text=MESSAGE,
        user_id="u-1", history=[], model="m", base_url=None, api_key=None,
        disable_thinking=False, session_memory=FakeMemory(),
        viewer_assembly={"status": "injected", "blocks": [{"kind": "selection"}]},
        research_turn=False, effective_handoff=None,
    )
    llm = FakeLLM(verdict=verdict)
    deps = SimpleNamespace(
        agent=SimpleNamespace(loop=SimpleNamespace(llm=llm)),
        retriever=FakeRetriever() if retriever is ... else retriever,
        viewer=_ViewerDeps(viewer_text),
    )
    return TurnRequest(
        ctx=ctx, deps=deps,
        plan=ExecutionPlan(kind=PlanKind.COMPOSITE, subrequests=("viewer_text", "private_recall")),
    )


async def _drain(req):
    return [e async for e in CompositeExecutor().stream(req, progress_sink=lambda e: None)]


async def test_relevant_answers_grounded_on_both_evidence_classes():
    req = _req()
    events = await _drain(req)
    assert [e["type"] for e in events] == ["content", "content", "done"]
    assert events[-1]["data"]["answer"] == "It steps downhill"
    system = req.deps.agent.loop.llm.stream_calls[0]["request"][0]["content"]
    assert "V1" in system and "[R1]" in system and "Gradient descent minimizes" in system
    assert req.deps.viewer.calls == [[{"kind": "selection"}]]  # SAME renderer, blocks untouched
    assert req.ctx.session_memory.appended[0] == ("user", MESSAGE)
    assert req.ctx.session_memory.appended[-1] == ("assistant", "It steps downhill")


async def test_cost_transparency_one_judge_call_one_generation_call():
    """Constraint 4: judge (chat) and generation (chat_stream) counted separately, 1+1."""
    req = _req()
    await _drain(req)
    llm = req.deps.agent.loop.llm
    assert len(llm.chat_calls) == 1     # the evaluation call, exactly once
    assert len(llm.stream_calls) == 1   # "ONE generation" = one final chat_stream
    assert llm.stream_calls[0]["tools"] is None


async def test_recall_uses_shared_seam_with_tenant_filter():
    req = _req()
    await _drain(req)
    query, _top_k, filters = req.deps.retriever.calls[0]
    assert query == MESSAGE and filters == {"user_id": "u-1"}
    assert len(req.deps.retriever.calls) == 1  # fans out once, no second pipeline


@pytest.mark.parametrize("verdict", ["irrelevant", "ambiguous", "unknown"])
async def test_non_affirmative_verdict_escalates_pre_commit(verdict):
    req = _req(verdict=verdict)
    with pytest.raises(EscalateToAgent) as ei:
        await _drain(req)
    assert "sufficiency" in ei.value.reason
    assert req.deps.agent.loop.llm.stream_calls == []  # no generation was paid for
    assert req.ctx.session_memory.appended == []


async def test_empty_recall_fails_closed():
    req = _req(retriever=FakeRetriever(hits=[]))
    with pytest.raises(EscalateToAgent) as ei:
        await _drain(req)
    assert "empty" in ei.value.reason


async def test_retrieval_failure_fails_closed():
    req = _req(retriever=FakeRetriever(raise_=RuntimeError("embedding down")))
    with pytest.raises(EscalateToAgent) as ei:
        await _drain(req)
    assert "retrieval failed" in ei.value.reason


async def test_run_path_returns_directresult():
    req = _req()
    result = await CompositeExecutor().run(req)
    assert result.final_answer == "It steps downhill"
    assert result.usage == {"total_tokens": 9} and result.error is None


async def test_empty_viewer_render_still_grounds_recall_section():
    req = _req(viewer_text="")
    events = await _drain(req)
    system = req.deps.agent.loop.llm.stream_calls[0]["request"][0]["content"]
    assert "## Retrieved evidence" in system and "[R1]" in system
    assert events[-1]["type"] == "done"
