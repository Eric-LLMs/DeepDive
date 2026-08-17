"""Memory retrieval: keyword + vector recall channels fused by RRF.

- ``KeywordRetriever`` is any deterministic keyword channel (local BM25 over the memdir,
  or PostgreSQL tsvector full-text over session messages).
- ``VectorRetriever`` is an optional embedding-based channel (pgvector cosine).
- :class:`RRFMemoryRetriever` fuses the two rankings; when the vector channel is missing
  or raises (embedding service offline), it degrades to the keyword channel — never a
  silent empty list.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Protocol

from agent.memory.base import MemoryStore
from agent.memory.types import Memory

_TOKEN_RE = re.compile(r"[a-z0-9\u4e00-\u9fff]+")

K1 = 1.2  # BM25 term-frequency saturation
B = 0.75  # BM25 length normalisation
DEFAULT_K = 60  # RRF constant


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


@dataclass
class MemoryHit:
    """A recalled memory (the carrier across keyword / vector / RRF stages)."""

    key: str
    content: str
    description: str = ""
    type: str = ""
    score: float = 0.0
    source: str = "keyword"  # "keyword" | "vector" | "rrf" | "file"
    meta: dict | None = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "content": self.content,
            "description": self.description,
            "type": self.type,
            "score": self.score,
            "source": self.source,
            "meta": self.meta,
        }


class KeywordRetriever(Protocol):
    """A deterministic keyword recall channel (tsvector / BM25)."""

    async def search(self, query: str, top_k: int = 5) -> list[MemoryHit]:
        ...


class VectorRetriever(Protocol):
    """An optional embedding-based recall channel (pgvector cosine)."""

    async def search(self, query: str, top_k: int = 5) -> list[MemoryHit]:
        ...


class KeywordIndex:
    """Deterministic BM25 index over a :class:`MemoryStore`. No external dependencies.

    The index is rebuilt per search over ``store.list()`` — fine for the small local
    memory corpus, and guarantees the result is always reproducible.
    """

    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    def _docs(self) -> list[tuple[str, Memory, list[str]]]:
        """(key, memory, tokens) triples for every memory."""
        return [
            (mem.name, mem, _tokenize(f"{mem.name} {mem.description} {mem.content}"))
            for mem in self._store.list()
        ]

    @staticmethod
    def _idf(term: str, df: int, n: int) -> float:
        if df == 0:
            return 0.0
        return math.log(1.0 + (n - df + 0.5) / (df + 0.5))

    async def search(self, query: str, top_k: int = 5) -> list[MemoryHit]:
        query_terms = _tokenize(query)
        if not query_terms:
            return []
        docs = self._docs()
        n = len(docs)
        if n == 0:
            return []
        avg_dl = sum(len(d) for _, _, d in docs) / n
        # document frequency per query term
        df = {t: sum(t in d for _, _, d in docs) for t in set(query_terms)}

        scored: list[tuple[float, Memory, str]] = []
        for key, mem, doc_tokens in docs:
            dl = len(doc_tokens) or 1
            tf = {t: doc_tokens.count(t) for t in query_terms}
            score = sum(
                self._idf(t, df[t], n)
                * (tf[t] * (K1 + 1.0)) / (tf[t] + K1 * (1.0 - B + B * dl / avg_dl))
                for t in query_terms
            )
            if score > 0:
                scored.append((score, mem, key))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [
            MemoryHit(
                key=key,
                content=mem.content,
                description=mem.description,
                type=mem.type,
                score=score,
                source="keyword",
            )
            for score, mem, key in scored[:top_k]
        ]


class RRFMemoryRetriever:
    """Fuses a keyword channel (tsvector / BM25) + a vector channel via RRF.

    If the vector channel is unavailable or raises (embedding service offline), returns
    the pure keyword result — a deterministic fallback, never a silent empty list.
    """

    def __init__(
        self,
        keyword: KeywordRetriever,
        vector: VectorRetriever | None = None,
        k: int = DEFAULT_K,
    ) -> None:
        self._keyword = keyword
        self._vector = vector
        self._k = k

    async def search(self, query: str, top_k: int = 5) -> list[MemoryHit]:
        from rag.rank.rrf import rrf_fusion
        from rag.types import SearchHit

        keyword_hits = await self._keyword.search(query, top_k=max(top_k * 2, 8))
        vector_hits: list[MemoryHit] = []
        if self._vector is not None:
            try:
                vector_hits = await self._vector.search(query, top_k=max(top_k * 2, 8))
            except Exception:  # noqa: BLE001 - vector down → deterministic keyword fallback
                vector_hits = []

        if not vector_hits:
            return keyword_hits[:top_k]

        kw_rank = [
            SearchHit(id=h.key, text=h.content, score=h.score, source="keyword")
            for h in keyword_hits
        ]
        vec_rank = [
            SearchHit(id=h.key, text=h.content, score=h.score, source="vector")
            for h in vector_hits
        ]
        fused = rrf_fusion([kw_rank, vec_rank], k=self._k)
        return [
            MemoryHit(
                key=h.id,
                content=h.text,
                description="",
                score=h.score,
                source="rrf",
                meta=h.meta,
            )
            for h in fused[:top_k]
        ]
