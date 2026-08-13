"""Vocabulary learning use cases: orchestrate ports to complete business flows.

The application layer depends only on ports interfaces; concrete implementations are injected by the upper layer (FastAPI).
"""
import csv
import io
import re
from uuid import UUID

from core.domain.models import Domain, Sentence, Term
from core.ports.images import ImagePort
from core.ports.llm import LLMPort
from core.ports.repositories import (
    DomainRepository,
    MatchRepository,
    SentenceRepository,
    TermRepository,
)
from core.ports.tts import TTSPort
from core.ports.vector import EmbeddingPort


class VocabularyService:
    def __init__(
        self,
        domains: DomainRepository,
        terms: TermRepository,
        sentences: SentenceRepository,
        matches: MatchRepository,
        llm: LLMPort,
        tts: TTSPort,
        images: ImagePort,
        embedder: EmbeddingPort | None = None,
    ) -> None:
        self.domains = domains
        self.terms = terms
        self.sentences = sentences
        self.matches = matches
        self.llm = llm
        self.tts = tts
        self.images = images
        self.embedder = embedder

    # ── Domains ──
    async def add_domain(self, name: str) -> Domain:
        return await self.domains.add(name)

    async def list_domains(self) -> list[Domain]:
        return await self.domains.list_all()

    # ── Terms ──
    async def add_term(self, domain_id: UUID, word: str, definition: str = "") -> Term:
        return await self.terms.add(domain_id, word, definition)

    async def list_terms(self, domain_id: UUID, only_active: bool = False) -> list[Term]:
        return await self.terms.list_by_domain(domain_id, only_active)

    async def update_term(
        self,
        term_id: UUID,
        definition: str | None = None,
        audio_hash: str | None = None,
        star_level: int | None = None,
        image_paths: list[str] | None = None,
        is_active: bool | None = None,
        frequency: int | None = None,
    ) -> None:
        await self.terms.update(
            term_id, definition, audio_hash, star_level, image_paths, is_active, frequency
        )

    async def bulk_update_terms(self, updates: list[dict]) -> None:
        await self.terms.bulk_update(updates)

    async def import_terms(self, domain_id: UUID, text: str) -> dict:
        items = self._parse_terms(text)
        return await self.import_terms_structured(domain_id, items)

    async def import_terms_structured(
        self, domain_id: UUID, items: list[tuple[str, str, int, int]]
    ) -> dict:
        added, skipped = await self.terms.bulk_add(domain_id, items)
        if added:
            await self._auto_link(domain_id)
        return {"added": added, "skipped": skipped}

    @staticmethod
    def _parse_terms(text: str) -> list[tuple[str, str, int, int]]:
        """Parse CSV/plain text into (word, definition, frequency, star_level) rows.

        Accepted line formats (one per line):
          word
          word,definition
          word,definition,frequency
          word,definition,frequency,star_level
        """
        items: list[tuple[str, str, int, int]] = []
        for row in csv.reader(io.StringIO(text)):
            row = [c.strip() for c in row]
            if not row or not row[0]:
                continue
            word = row[0]
            definition = row[1] if len(row) > 1 else ""
            frequency = int(row[2]) if len(row) > 2 and row[2].isdigit() else 1
            star_level = int(row[3]) if len(row) > 3 and row[3].isdigit() else 1
            items.append((word, definition, frequency, star_level))
        return items

    # ── Sentences ──
    async def add_sentence(self, domain_id: UUID, content_en: str) -> Sentence:
        return await self.sentences.add(domain_id, content_en)

    async def update_sentence(
        self, sentence_id: UUID, content_cn: str | None = None, audio_hash: str | None = None
    ) -> None:
        await self.sentences.update(sentence_id, content_cn, audio_hash)

    async def list_sentences(self, domain_id: UUID) -> list[Sentence]:
        return await self.sentences.list_by_domain(domain_id)

    async def search_sentences(self, domain_id: UUID, term_text: str) -> list[Sentence] | list[dict]:
        # Hybrid search: keyword first, fall back to semantic when there are no hits
        # (mirrors the old demo's search_sentences_hybrid).
        keyword = await self.sentences.search_by_text(domain_id, term_text)
        if keyword or self.embedder is None:
            return keyword
        return await self.search_sentences_semantic(domain_id, term_text)

    async def import_sentences(self, domain_id: UUID, text: str) -> dict:
        sentences = self._split_sentences(text)
        return await self.import_sentences_structured(domain_id, sentences)

    async def import_sentences_structured(self, domain_id: UUID, sentences: list[str]) -> dict:
        added, skipped = await self.sentences.bulk_add(domain_id, sentences)
        if added:
            await self._auto_link(domain_id)
        return {"added": added, "skipped": skipped}

    @staticmethod
    def _split_sentences(text: str) -> list[str]:
        """Split raw text into sentences (by . ! ?), dropping fragments shorter than 5 chars."""
        parts = re.split(r"(?<=[.!?])\s+", text.strip())
        return [p.strip() for p in parts if len(p.strip()) >= 5]

    # ── Semantic search ──
    async def index_sentences(self, domain_id: UUID) -> dict:
        if self.embedder is None:
            return {"indexed": 0, "error": "embedding unavailable (install [rag] extra)"}
        sentences = await self.sentences.list_by_domain(domain_id)
        if not sentences:
            return {"indexed": 0}
        try:
            embeddings = await self.embedder.embed([s.content_en for s in sentences])
        except Exception as exc:  # model not installed / failed to load → degrade gracefully
            return {"indexed": 0, "error": f"embedding failed: {exc}"}
        for s, emb in zip(sentences, embeddings):
            await self.sentences.set_embedding(s.id, emb)
        return {"indexed": len(sentences)}

    async def search_sentences_semantic(
        self, domain_id: UUID, query: str, top_k: int = 10
    ) -> list[dict]:
        if self.embedder is None:
            return []
        try:
            q_emb = (await self.embedder.embed([query]))[0]
        except Exception:  # embeddings unavailable → no semantic results
            return []
        return await self.sentences.search_semantic(domain_id, q_emb, top_k)

    # ── AI capabilities ──
    async def explain_term_in_context(self, term: str, context: str) -> dict:
        return await self.llm.explain_term(term, context)

    async def generate_definition(self, term: str) -> str:
        return await self.llm.generate_definition(term)

    async def analyze_syntax(self, sentence: str) -> str:
        return await self.llm.analyze_syntax(sentence)

    async def synthesize_audio(self, text: str) -> str | None:
        return await self.tts.synthesize(text)

    # ── Images ──
    async def fetch_term_images(
        self, word: str, definition: str = "", context: str = "", regenerate: bool = False
    ) -> list[str]:
        return await self.images.fetch(word, definition, context, regenerate)

    # ── Relations ──
    async def _auto_link(self, domain_id: UUID) -> None:
        """Auto-link every term to sentences that contain it.

        Mirrors the old demo's IngestionEngine: for each term, scan every sentence in
        the domain using a ``\\b``-bounded regex (case-insensitive) so that e.g. "apple"
        does not match "pineapple". Idempotent — existing links are left untouched.
        """
        terms = await self.terms.list_by_domain(domain_id)
        sentences = await self.sentences.list_by_domain(domain_id)
        if not terms or not sentences:
            return
        for term in terms:
            pattern = re.compile(r"\b" + re.escape(term.word) + r"\b", re.IGNORECASE)
            for sentence in sentences:
                if pattern.search(sentence.content_en):
                    await self.matches.add(term.id, sentence.id)

    async def link_term_to_sentence(
        self, term_id: UUID, sentence_id: UUID, explanation: str | None = None
    ) -> None:
        await self.matches.add(term_id, sentence_id, explanation)

    async def list_sentences_for_term(self, term_id: UUID) -> list[dict]:
        return await self.matches.list_for_term(term_id)
