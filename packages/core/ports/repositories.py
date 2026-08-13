"""Repository ports: the domain layer reads/writes data through them, without depending on a concrete store."""
from typing import Protocol
from uuid import UUID

from core.domain.models import Domain, Match, Sentence, Term


class DomainRepository(Protocol):
    async def add(self, name: str) -> Domain:
        """Add a domain (returns the existing record on name conflict)."""
        ...

    async def list_all(self) -> list[Domain]:
        ...


class TermRepository(Protocol):
    async def add(
        self, domain_id: UUID, word: str, definition: str = "", frequency: int = 1, star_level: int = 1
    ) -> Term:
        """Add a term (case-insensitive dedup within the same domain)."""
        ...

    async def list_by_domain(self, domain_id: UUID, only_active: bool = False) -> list[Term]:
        ...

    async def bulk_update(self, updates: list[dict]) -> None:
        ...

    async def bulk_add(
        self, domain_id: UUID, items: list[tuple[str, str, int, int]]
    ) -> tuple[int, int]:
        """Bulk-add terms (case-insensitive dedup within the domain); returns (added, skipped)."""
        ...

    async def update(
        self,
        term_id: UUID,
        definition: str | None = None,
        audio_hash: str | None = None,
        star_level: int | None = None,
        image_paths: list[str] | None = None,
        is_active: bool | None = None,
        frequency: int | None = None,
    ) -> None:
        """Update a term's definition/audio/star level/images/active flag/frequency (only non-None fields are updated)."""
        ...


class SentenceRepository(Protocol):
    async def add(self, domain_id: UUID, content_en: str) -> Sentence:
        """Add a sentence (deduped by unique content_en)."""
        ...

    async def list_by_domain(self, domain_id: UUID) -> list[Sentence]:
        ...

    async def bulk_add(self, domain_id: UUID, sentences: list[str]) -> tuple[int, int]:
        """Bulk-add sentences (deduped by unique content_en); returns (added, skipped)."""
        ...

    async def update(self, sentence_id: UUID, content_cn: str | None = None, audio_hash: str | None = None) -> None:
        ...

    async def search_by_text(self, domain_id: UUID, term_text: str) -> list[Sentence]:
        ...

    async def set_embedding(self, sentence_id: UUID, embedding: list[float]) -> None:
        ...

    async def search_semantic(
        self, domain_id: UUID, query_embedding: list[float], top_k: int = 10
    ) -> list[dict]:
        """Cosine-similarity search over sentence embeddings, returns [{...sentence, score}]."""
        ...


class MatchRepository(Protocol):
    async def add(self, term_id: UUID, sentence_id: UUID, cn_explanation: str | None = None) -> None:
        """Create a term↔sentence relation (updates the explanation if it already exists)."""
        ...

    async def list_for_term(self, term_id: UUID) -> list[dict]:
        """Return [{...sentence, cn_explanation}]."""
        ...
