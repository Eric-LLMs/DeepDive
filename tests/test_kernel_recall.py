"""Tests for the kernel's proactive recall section (DYNAMIC_SUFFIX injection).

Verifies the section renders top hits for the user's message, caches per turn (so per-step
refresh doesn't hit the recall channels repeatedly), skips empty queries, and that a new
turn resets the cache so each turn recomputes. Recall is turn-scoped (``AgentTurn.recall_hits``)
— the old shared ``kernel._recall_hits`` field is gone.
"""
from agent import ToolRuntime
from agent.context import AgentTurn
from agent.harness import FakeLLM, assistant
from agent.kernel import AgentKernel
from agent.memory.retrieval import MemoryHit


class _StubMemory:
    def __init__(self, hits=None, *, should_recall=True):
        self.hits = hits or []
        self.queries = []
        self._should_recall = should_recall

    def begin_session(self):
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


def _ctx(msg: str) -> dict:
    """Assemble-context for the recall section, carrying a fresh per-turn AgentTurn."""
    return {"user_msg": msg, "turn": AgentTurn(user_msg=msg)}


async def test_proactive_recall_injects_hits_and_caches_within_a_run():
    mem = _StubMemory([MemoryHit(key="k", content="the attention mechanism attends to inputs")])
    kernel = _kernel(mem)

    ctx = _ctx("what is attention?")
    text = await kernel._memory_recall_section(ctx)

    assert text == "## Recalled memory\n- the attention mechanism attends to inputs"
    assert mem.queries == ["what is attention?"]

    # a second resolve within the same turn reuses the cache (no extra recall)
    again = await kernel._memory_recall_section(ctx)
    assert again == text
    assert mem.queries == ["what is attention?"]


async def test_proactive_recall_skips_empty_query():
    mem = _StubMemory([MemoryHit(key="k", content="x")])
    kernel = _kernel(mem)

    assert await kernel._memory_recall_section(_ctx("   ")) == ""
    assert mem.queries == []


async def test_proactive_recall_renders_nothing_when_no_hits():
    kernel = _kernel(_StubMemory())

    assert await kernel._memory_recall_section(_ctx("hi")) == ""


async def test_proactive_recall_skips_when_gate_says_no():
    mem = _StubMemory([MemoryHit(key="k", content="note")], should_recall=False)
    kernel = _kernel(mem)

    assert await kernel._memory_recall_section(_ctx("explain the code")) == ""
    assert mem.queries == []  # no deep recall ran on a non-memory-seeking turn


async def test_recall_is_turn_scoped_and_recomputed_per_run():
    mem = _StubMemory([MemoryHit(key="k", content="note")])
    kernel = _kernel(mem)

    # the cache lives on the AgentTurn, not the kernel: two turns recompute independently
    ctx1 = _ctx("first question")
    assert await kernel._memory_recall_section(ctx1) != ""
    assert await kernel._memory_recall_section(ctx1) != ""  # cached within the turn
    assert mem.queries == ["first question"]

    ctx2 = _ctx("second question")
    assert await kernel._memory_recall_section(ctx2) != ""
    assert mem.queries == ["first question", "second question"]


async def test_run_builds_fresh_turn_each_time():
    mem = _StubMemory([MemoryHit(key="k", content="note")])
    kernel = _kernel(mem)

    await kernel.run("first question")
    await kernel.run("second question")

    # each run builds a fresh AgentTurn → recall recomputed from the new turn's message
    assert mem.queries == ["first question", "second question"]
