"""Tests for the recall gate (``MemoryService.should_recall``).

The lexical prefilter decides whether proactive recall runs on a turn: memory-seeking
phrases (bilingual), or short/elliptical queries that plausibly refer to prior context.
Blank and neutral non-short queries skip deep recall.
"""
from agent.memory.file import FileMemoryStore
from agent.memory.service import MemoryService


class _FakeRetriever:
    async def search(self, query, top_k=5):
        return []


def _service(tmp_path):
    return MemoryService(FileMemoryStore(tmp_path), _FakeRetriever())


async def test_recall_fires_on_memory_seeking_phrases(tmp_path):
    service = _service(tmp_path)
    for q in (
        "记得我们上次聊的注意力机制吗",
        "你之前说过要改这个",
        "remember what we discussed earlier",
        "last time we talked about the API",
        "上次呢",
    ):
        assert service.should_recall(q), f"expected recall for {q!r}"


async def test_recall_skips_blank_and_neutral_queries(tmp_path):
    service = _service(tmp_path)
    for q in ("   ", "", "什么是注意力机制", "how does the router work", "给我解释一下这段代码"):
        assert not service.should_recall(q), f"expected no recall for {q!r}"


async def test_short_elliptical_queries_always_recall(tmp_path):
    service = _service(tmp_path)
    # a 2-char follow-up can only make sense against prior context
    assert service.should_recall("那个呢")
    assert service.should_recall("接着")
