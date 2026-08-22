"""Tests for the kernel's proactive recall section (DYNAMIC_SUFFIX injection).

Verifies the section renders top hits for the user's message, caches per run (so per-step
refresh doesn't hit the recall channels repeatedly), skips empty queries, and that a new
run resets the cache so each turn recomputes.
"""
from agent import ToolRuntime
from agent.harness import FakeLLM, assistant
from agent.kernel import AgentKernel
from agent.memory.retrieval import MemoryHit


class _StubMemory:
    def __init__(self, hits=None, *, should_recall=True):
        self.hits = hits or []
        self.queries = []
        self._should_recall = should_recall

    def begin_session(self):
        pass

    def session_brief(self):
        return ""

    def should_recall(self, query):
        return self._should_recall

    async def recall_all(self, query, top_k):
        self.queries.append(query)
        return self.hits

    async def recall_file(self, query, limit):
        return []

    async def save(self, *args, **kwargs):
        pass


def _kernel(memory):
    return AgentKernel(FakeLLM([assistant("ok")]), ToolRuntime(), memory=memory)


async def test_proactive_recall_injects_hits_and_caches_within_a_run():
    mem = _StubMemory([MemoryHit(key="k", content="the attention mechanism attends to inputs")])
    kernel = _kernel(mem)

    text = await kernel._memory_recall_section({"user_msg": "what is attention?"})

    assert text == "## Recalled memory\n- the attention mechanism attends to inputs"
    assert mem.queries == ["what is attention?"]

    # a second resolve within the same run reuses the cache (no extra recall)
    again = await kernel._memory_recall_section({"user_msg": "what is attention?"})
    assert again == text
    assert mem.queries == ["what is attention?"]


async def test_proactive_recall_skips_empty_query():
    mem = _StubMemory([MemoryHit(key="k", content="x")])
    kernel = _kernel(mem)

    assert await kernel._memory_recall_section({"user_msg": "   "}) == ""
    assert mem.queries == []


async def test_proactive_recall_renders_nothing_when_no_hits():
    kernel = _kernel(_StubMemory())

    assert await kernel._memory_recall_section({"user_msg": "hi"}) == ""


async def test_proactive_recall_skips_when_gate_says_no():
    mem = _StubMemory([MemoryHit(key="k", content="note")], should_recall=False)
    kernel = _kernel(mem)

    assert await kernel._memory_recall_section({"user_msg": "explain the code"}) == ""
    assert mem.queries == []  # no deep recall ran on a non-memory-seeking turn


async def test_run_resets_recall_cache_for_next_turn():
    mem = _StubMemory([MemoryHit(key="k", content="note")])
    kernel = _kernel(mem)
    kernel._recall_hits = ["stale sentinel"]

    await kernel.run("first question")

    assert kernel._recall_hits == [MemoryHit(key="k", content="note")]
    assert mem.queries == ["first question"]  # recomputed from the new turn's message
