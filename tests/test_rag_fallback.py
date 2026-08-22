"""Tests for the rag_search fast-degradation path.

When the retrieval stack is down (embedding 503 etc.), the tool must return a normal
(successful) result telling the model to answer from knowledge, instead of raising so
the runtime surfaces a tool error that burns another model round on a pointless retry.
"""
import httpx
from agent.decisions import ToolExecution, ToolExecutionSuccess
from agent.runtime import ToolRuntime

from apps.api.tools.rag_search_tool import _UNAVAILABLE, register


class _Ctx:
    def __init__(self, retriever):
        self._retriever = retriever

    def resolve(self, key):
        assert key == "retrieval"
        return self._retriever


class _Down:
    async def retrieve(self, query, top_k, filters):
        raise httpx.HTTPStatusError(
            "503 Service Unavailable",
            request=httpx.Request("POST", "http://embedding/embed"),
            response=httpx.Response(503),
        )


class _Up:
    async def retrieve(self, query, top_k, filters):
        return [{"id": "c1", "text": "attention weights attend to inputs", "score": 0.9, "meta": {}}]


def _tool(retriever):
    runtime = ToolRuntime()
    register(runtime, _Ctx(retriever), None)
    return runtime


async def test_retrieval_failure_degrades_to_knowledge_only_notice():
    runtime = _tool(_Down())

    result = await runtime.execute(
        ToolExecution(call_id="1", name="rag_search", arguments={"query": "what is attention?"})
    )

    assert isinstance(result, ToolExecutionSuccess)
    assert result.value == _UNAVAILABLE


async def test_retrieval_success_returns_chunks():
    runtime = _tool(_Up())

    result = await runtime.execute(
        ToolExecution(call_id="1", name="rag_search", arguments={"query": "what is attention?"})
    )

    assert isinstance(result, ToolExecutionSuccess)
    assert result.value == [
        {"id": "c1", "text": "attention weights attend to inputs", "score": 0.9, "meta": {}}
    ]
