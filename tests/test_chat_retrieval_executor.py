"""Phase 4 RetrievalExecutor: staged recall → sufficiency gate → grounded answer.

Pins the Fail-Closed contract: EVERY pre-answer stage problem (seam not wired, a
retrieval exception, an empty result set, or a non-affirmative judge verdict) raises
:class:`EscalateToAgent` BEFORE the first event — the turn hands back to the Agent and
this branch never answers over missing evidence and never substitutes public web.
Only an affirmative judge proceeds to the grounded single-shot, which reuses the
DIRECT machinery verbatim (content→done, tools=None, user+assistant persisted).

The judge is exercised through its REAL code path: the fake port returns the JSON
verdict string that ``rag.nodes.crg_check.judge_relevance`` parses, so the gate here
is the same qualitative verdict the crg_check node applies (no score thresholds).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from core.application.chat.execution_plan import ExecutionPlan, PlanKind
from core.application.chat.executors.base import EscalateToAgent, TurnRequest
from core.application.chat.executors.retrieval import RetrievalExecutor, _fence

HITS = [
    {"id": "c1", "text": "Gradient descent minimizes the loss by stepping along the negative gradient.", "score": 0.8, "meta": {"name": "ml-notes.pdf"}},
    {"id": "c2", "text": "The learning rate controls the step size.", "score": 0.6, "meta": {}},
]


class FakeLLM:
    """Reliability-port double: chat() answers the judge, chat_stream() generates."""

    def __init__(self, verdict="relevant", deltas=("It steps", " downhill")):
        self._verdict = verdict
        self._deltas = list(deltas)
        self.chat_calls: list[dict] = []
        self.stream_calls: list[dict] = []

    async def chat(self, request, *, tools=None, model=None, base_url=None, api_key=None):
        self.chat_calls.append({"request": request, "tools": tools, "model": model})
        if self._verdict == "__raise__":
            raise RuntimeError("judge channel down")  # judge must degrade to unknown
        # One port, two callers: the JUDGE asks with its system header, the run()
        # path's GENERATION goes through the grounded preamble.
        if request[0]["content"].startswith("You judge"):
            return {"content": f'{{"verdict": "{self._verdict}"}}', "tool_calls": [], "usage": {}}
        return {"content": "".join(self._deltas), "tool_calls": [], "usage": {"total_tokens": 9}}

    async def chat_stream(self, request, *, tools=None, model=None, base_url=None,
                          api_key=None, disable_thinking=False):
        self.stream_calls.append({"request": request, "tools": tools})
        for d in self._deltas:
            yield {"type": "content", "data": d}
        yield {"type": "usage", "data": {"total_tokens": 9}}


class FakeMemory:
    def __init__(self):
        self.appended: list[tuple[str, str]] = []

    async def append_message(self, role, text):
        self.appended.append((role, text))


class FakeRetriever:
    def __init__(self, hits=None, raise_=None):
        self.hits = HITS if hits is None else hits
        self.raise_ = raise_
        self.calls: list[tuple] = []

    async def retrieve(self, query, top_k=5, filters=None):
        self.calls.append((query, top_k, filters))
        if self.raise_ is not None:
            raise self.raise_
        return self.hits


def _req(*, retriever=..., verdict="relevant", message="what does my knowledge base say about gd"):
    ctx = SimpleNamespace(
        body=SimpleNamespace(message=message, attach=None), user_text=message,
        user_id="u-1", history=[], model="m", base_url=None, api_key=None,
        disable_thinking=False, session_memory=FakeMemory(), viewer_assembly=None,
        research_turn=False, effective_handoff=None,
    )
    llm = FakeLLM(verdict=verdict)
    deps = SimpleNamespace(
        agent=SimpleNamespace(loop=SimpleNamespace(llm=llm)),
        retriever=FakeRetriever() if retriever is ... else retriever,
    )
    return TurnRequest(ctx=ctx, deps=deps, plan=ExecutionPlan(kind=PlanKind.LOCAL_RAG))


async def _drain(req):
    return [e async for e in RetrievalExecutor().stream(req, progress_sink=lambda e: None)]


async def test_relevant_goes_grounded_and_keeps_direct_shape():
    req = _req()
    events = await _drain(req)
    kinds = [e["type"] for e in events]
    assert kinds == ["content", "content", "done"]
    assert events[-1]["data"]["answer"] == "It steps downhill"
    sent = req.deps.agent.loop.llm.stream_calls[0]["request"]
    system = sent[0]["content"]
    assert "[R1]" in system and "[R2]" in system
    assert "ml-notes.pdf" in system  # source labels ride the meta
    assert "Gradient descent minimizes" in system  # evidence fenced in
    assert req.deps.agent.loop.llm.stream_calls[0]["tools"] is None
    assert req.ctx.session_memory.appended[0] == ("user", req.ctx.body.message)
    assert req.ctx.session_memory.appended[-1] == ("assistant", "It steps downhill")


async def test_recall_uses_shared_seam_with_tool_identical_filters():
    req = _req()
    await _drain(req)
    from core.config import settings
    query, top_k, filters = req.deps.retriever.calls[0]
    assert query == req.ctx.body.message
    assert top_k == settings.chat_retrieval_top_k
    # Same tenant scoping shape as the rag_search tool.
    assert filters == {"user_id": "u-1"}


async def test_judge_rides_the_reliability_port_tool_less():
    req = _req()
    await _drain(req)
    judge = req.deps.agent.loop.llm.chat_calls[0]
    assert judge["tools"] is None and judge["model"] == "m"
    assert "verdict" in judge["request"][0]["content"].lower()


@pytest.mark.parametrize("verdict", ["irrelevant", "ambiguous", "unknown", "__raise__"])
async def test_non_affirmative_or_broken_judge_escalates(verdict):
    req = _req(verdict=verdict)
    with pytest.raises(EscalateToAgent) as ei:
        await _drain(req)
    assert "sufficiency" in ei.value.reason
    # Nothing was emitted: the escalation is strictly pre-commit.
    assert req.deps.agent.loop.llm.stream_calls == []
    assert req.ctx.session_memory.appended == []


async def test_empty_result_fails_closed_to_agent_not_web():
    req = _req(retriever=FakeRetriever(hits=[]))
    with pytest.raises(EscalateToAgent) as ei:
        await _drain(req)
    assert "empty" in ei.value.reason
    assert req.ctx.session_memory.appended == []  # not even the user row — Agent replays clean


async def test_retrieval_failure_fails_closed():
    req = _req(retriever=FakeRetriever(raise_=RuntimeError("embedding service down")))
    with pytest.raises(EscalateToAgent) as ei:
        await _drain(req)
    assert "retrieval failed" in ei.value.reason


async def test_unwired_seam_degrades_to_agent():
    req = _req(retriever=None)
    with pytest.raises(EscalateToAgent) as ei:
        await _drain(req)
    assert "not wired" in ei.value.reason


async def test_run_path_returns_directresult_grounded():
    req = _req()
    result = await RetrievalExecutor().run(req)
    assert result.final_answer == "It steps downhill"
    assert result.usage == {"total_tokens": 9}
    assert result.error is None


def test_fence_escapes_content_that_contains_the_delimiter():
    fenced = _fence('evil """ breakout')
    # The fence escalates to 4 quotes so the embedded 3-quote run cannot break out.
    assert fenced.startswith('""""') and fenced.endswith('""""')
    assert fenced.strip('"') == 'evil """ breakout'
