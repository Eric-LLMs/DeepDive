"""Tests for memory recall: BM25 keyword + vector fusion by RRF, with deterministic fallback.

Verifies that the RRF retriever fuses the keyword and vector channels, and that a failing
vector channel (embedding service offline) degrades to the keyword result — never an empty
list.
"""

from agent.memory.retrieval import KeywordIndex, MemoryHit, RRFMemoryRetriever
from agent.memory.types import Memory


class FakeStore:
    def __init__(self, memories: list[Memory]) -> None:
        self._memories = memories

    def list(self) -> list[Memory]:
        return self._memories


class FakeVector:
    def __init__(self, hits: list[MemoryHit] | None = None, *, raise_error: bool = False) -> None:
        self._hits = hits or []
        self._raise = raise_error

    async def search(self, query: str, top_k: int = 5) -> list[MemoryHit]:
        if self._raise:
            raise RuntimeError("embedding service offline")
        return self._hits[:top_k]


def _memory(name: str, content: str) -> Memory:
    return Memory(name=name, content=content, description="")


async def test_keyword_index_is_deterministic():
    store = FakeStore(
        [
            _memory("deploy", "The deployment pipeline runs on Jenkins every night."),
            _memory("auth", "Users authenticate with a JWT in the Authorization header."),
            _memory("memory", "Memory recall fuses keyword and vector scores by RRF."),
        ]
    )
    index = KeywordIndex(store)

    hits = await index.search("jwt authenticate users", top_k=3)
    assert hits
    assert hits[0].key == "auth"
    assert hits[0].source == "keyword"
    assert all(h.score > 0 for h in hits)

    again = await index.search("jwt authenticate users", top_k=3)
    assert [h.key for h in again] == [h.key for h in hits]  # reproducible


async def test_rrf_fuses_keyword_and_vector_channels():
    store = FakeStore(
        [
            _memory("deploy", "Deployment runs nightly on the Jenkins pipeline."),
            _memory("auth", "Authentication uses JWT bearer tokens."),
        ]
    )
    keyword = KeywordIndex(store)
    vector = FakeVector(
        [
            MemoryHit(key="auth", content="Authentication uses JWT bearer tokens.", score=0.9, source="vector"),
            MemoryHit(key="deploy", content="Deployment runs nightly.", score=0.4, source="vector"),
        ]
    )
    retriever = RRFMemoryRetriever(keyword=keyword, vector=vector)

    hits = await retriever.search("auth jwt", top_k=2)

    assert hits
    assert hits[0].key == "auth"  # strong on both channels → top fused hit
    assert hits[0].source == "rrf"


async def test_vector_failure_falls_back_to_keyword_not_empty():
    store = FakeStore(
        [
            _memory("deploy", "The deployment pipeline runs on Jenkins every night."),
            _memory("auth", "Users authenticate with a JWT in the Authorization header."),
        ]
    )
    keyword = KeywordIndex(store)
    vector = FakeVector(raise_error=True)
    retriever = RRFMemoryRetriever(keyword=keyword, vector=vector)

    hits = await retriever.search("jenkins deployment", top_k=2)

    assert hits  # deterministic keyword fallback, never a silent empty
    assert hits[0].key == "deploy"
    assert hits[0].source == "keyword"


async def test_rrf_without_vector_uses_keyword_only():
    store = FakeStore([_memory("deploy", "Deployment runs nightly on Jenkins.")])
    retriever = RRFMemoryRetriever(keyword=KeywordIndex(store))

    hits = await retriever.search("jenkins", top_k=1)
    assert hits[0].key == "deploy"
