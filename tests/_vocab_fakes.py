"""Hand-rolled fakes for vocabulary tests (no DB), mirroring tests/_drive_fakes.py style.

Implement the same method surface as the SQL repositories so VocabularyService runs
unchanged against them. Ids are sequential UUIDs (1, 2, 3 …) for deterministic asserts.
"""
from __future__ import annotations

from uuid import UUID, uuid4

from core.domain.models import Domain, Sentence, Term


def _uuid(n: int) -> UUID:
    return UUID(int=n)


class FakeDomains:
    def __init__(self) -> None:
        self.rows: list[Domain] = []
        self._seq = 0

    def _next(self) -> UUID:
        self._seq += 1
        return _uuid(self._seq)

    async def add(self, name: str, user_id: UUID | None = None) -> Domain:
        for d in self.rows:
            if d.name == name and (
                (d.user_id is None and user_id is None) or d.user_id == user_id
            ):
                return d
        d = Domain(id=self._next(), name=name, user_id=user_id)
        self.rows.append(d)
        return d

    async def get(self, domain_id: UUID) -> Domain | None:
        return next((d for d in self.rows if d.id == domain_id), None)

    async def list_all(self, user_id: UUID | None = None) -> list[Domain]:
        return [d for d in self.rows if d.user_id is None or d.user_id == user_id]


class FakeTerms:
    def __init__(self) -> None:
        self.rows: list[Term] = []
        self._seq = 0

    def _next(self) -> UUID:
        self._seq += 1
        return _uuid(self._seq)

    async def add(
        self, domain_id: UUID, word: str, definition: str = "", frequency: int = 1,
        star_level: int = 1,
    ) -> Term:
        for t in self.rows:
            if t.domain_id == domain_id and t.word.lower() == word.lower():
                return t
        t = Term(
            id=self._next(), domain_id=domain_id, word=word, definition=definition,
            frequency=frequency, star_level=star_level,
        )
        self.rows.append(t)
        return t

    async def get(self, term_id: UUID) -> Term | None:
        return next((t for t in self.rows if t.id == term_id), None)

    async def list_by_domain(self, domain_id: UUID, only_active: bool = False) -> list[Term]:
        rows = [t for t in self.rows if t.domain_id == domain_id]
        if only_active:
            rows = [t for t in rows if t.is_active]
        return rows

    async def bulk_add(
        self, domain_id: UUID, items: list[tuple[str, str, int, int]]
    ) -> tuple[int, int]:
        added, skipped = 0, 0
        for word, definition, frequency, star_level in items:
            if any(t.domain_id == domain_id and t.word.lower() == word.lower() for t in self.rows):
                skipped += 1
                continue
            self.rows.append(
                Term(
                    id=self._next(), domain_id=domain_id, word=word, definition=definition,
                    frequency=frequency, star_level=star_level,
                )
            )
            added += 1
        return added, skipped

    async def update(self, term_id: UUID, **kwargs) -> None:
        t = next((t for t in self.rows if t.id == term_id), None)
        if t is not None:
            for k, v in kwargs.items():
                if v is not None:
                    setattr(t, k, v)

    async def bulk_update(self, updates: list[dict]) -> None:
        for u in updates:
            t = next((t for t in self.rows if t.id == u["id"]), None)
            if t is not None:
                for k in ("word", "definition", "star_level", "is_active", "frequency"):
                    if k in u and u[k] is not None:
                        setattr(t, k, u[k])


class FakeSentences:
    def __init__(self) -> None:
        self.rows: list[Sentence] = []
        self._seq = 0

    def _next(self) -> UUID:
        self._seq += 1
        return _uuid(self._seq)

    async def add(self, domain_id: UUID, content_en: str, user_id: UUID | None = None) -> Sentence:
        for s in self.rows:
            if s.content_en == content_en and (
                (s.user_id is None and user_id is None) or s.user_id == user_id
            ):
                return s
        s = Sentence(id=self._next(), domain_id=domain_id, content_en=content_en, user_id=user_id)
        self.rows.append(s)
        return s

    async def get(self, sentence_id: UUID) -> Sentence | None:
        return next((s for s in self.rows if s.id == sentence_id), None)

    async def list_by_domain(self, domain_id: UUID, user_id: UUID | None = None) -> list[Sentence]:
        rows = [s for s in self.rows if s.domain_id == domain_id]
        if user_id is not None:
            rows = [s for s in rows if s.user_id is None or s.user_id == user_id]
        return rows

    async def bulk_add(
        self, domain_id: UUID, sentences: list[str], user_id: UUID | None = None
    ) -> tuple[int, int]:
        added, skipped = 0, 0
        for content in sentences:
            if any(
                s.content_en == content and (
                    (s.user_id is None and user_id is None) or s.user_id == user_id
                )
                for s in self.rows
            ):
                skipped += 1
                continue
            self.rows.append(
                Sentence(id=self._next(), domain_id=domain_id, content_en=content, user_id=user_id)
            )
            added += 1
        return added, skipped

    async def update(self, sentence_id: UUID, content_cn: str | None = None, audio_hash: str | None = None) -> None:
        s = next((s for s in self.rows if s.id == sentence_id), None)
        if s is not None:
            if content_cn is not None:
                s.content_cn = content_cn
            if audio_hash is not None:
                s.audio_hash = audio_hash

    async def search_by_text(self, domain_id: UUID, term_text: str, user_id: UUID | None = None) -> list[Sentence]:
        rows = [s for s in self.rows if s.domain_id == domain_id and term_text in s.content_en]
        if user_id is not None:
            rows = [s for s in rows if s.user_id is None or s.user_id == user_id]
        return rows

    async def set_embedding(self, sentence_id: UUID, embedding: list[float]) -> None:
        pass

    async def search_semantic(
        self, domain_id: UUID, query_embedding: list[float], top_k: int = 10,
        user_id: UUID | None = None,
    ) -> list[dict]:
        return []


class FakeMatches:
    def __init__(self, sentences: FakeSentences) -> None:
        self.sentences = sentences
        self.rows: list[dict] = []

    async def add(
        self, term_id: UUID, sentence_id: UUID, cn_explanation: str | None = None
    ) -> None:
        for m in self.rows:
            if m["term_id"] == term_id and m["sentence_id"] == sentence_id:
                if cn_explanation is not None:
                    m["cn_explanation"] = cn_explanation
                return
        self.rows.append(
            {"term_id": term_id, "sentence_id": sentence_id, "cn_explanation": cn_explanation}
        )

    async def list_for_term(self, term_id: UUID) -> list[dict]:
        out = []
        for m in self.rows:
            if m["term_id"] != term_id:
                continue
            s = next((x for x in self.sentences.rows if x.id == m["sentence_id"]), None)
            if s is None:
                continue
            item = s.model_dump()
            item["cn_explanation"] = m["cn_explanation"]
            out.append(item)
        return out


def make_vocab():
    """VocabularyService wired to in-memory fakes (AI clients stubbed with None)."""
    from core.application.services import VocabularyService

    domains = FakeDomains()
    terms = FakeTerms()
    sentences = FakeSentences()
    matches = FakeMatches(sentences)
    svc = VocabularyService(
        domains, terms, sentences, matches,
        llm=None, tts=None, images=None, embedder=None,
    )
    return svc, domains, terms, sentences, matches
