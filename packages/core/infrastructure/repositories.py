"""SQLAlchemy async repository implementations.

Deduplication is implemented via "select-then-insert" rather than UNIQUE constraints + catching IntegrityError.
"""
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.domain.models import Domain, Match, Sentence, Term
from core.infrastructure.db import (
    DomainModel,
    MatchModel,
    SentenceModel,
    TermModel,
)


class SqlDomainRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, name: str) -> Domain:
        row = (
            await self.session.execute(select(DomainModel).where(DomainModel.name == name))
        ).scalar_one_or_none()
        if row:
            return Domain.model_validate(row)
        obj = DomainModel(name=name)
        self.session.add(obj)
        await self.session.commit()
        await self.session.refresh(obj)
        return Domain.model_validate(obj)

    async def list_all(self) -> list[Domain]:
        rows = (await self.session.execute(select(DomainModel))).scalars().all()
        return [Domain.model_validate(r) for r in rows]


class SqlTermRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(
        self, domain_id: UUID, word: str, definition: str = "", frequency: int = 1, star_level: int = 1
    ) -> Term:
        row = (
            await self.session.execute(
                select(TermModel).where(
                    TermModel.domain_id == domain_id,
                    func.lower(TermModel.word) == word.lower(),
                )
            )
        ).scalar_one_or_none()
        if row:
            return Term.model_validate(row)
        obj = TermModel(
            domain_id=domain_id, word=word, definition=definition,
            frequency=frequency, star_level=star_level,
        )
        self.session.add(obj)
        await self.session.commit()
        await self.session.refresh(obj)
        return Term.model_validate(obj)

    async def list_by_domain(self, domain_id: UUID, only_active: bool = False) -> list[Term]:
        stmt = select(TermModel).where(TermModel.domain_id == domain_id)
        if only_active:
            stmt = stmt.where(TermModel.is_active.is_(True))
        rows = (await self.session.execute(stmt)).scalars().all()
        return [Term.model_validate(r) for r in rows]

    async def bulk_update(self, updates: list[dict]) -> None:
        for u in updates:
            values = {
                field: u[field]
                for field in ("word", "definition", "star_level", "is_active", "frequency")
                if u.get(field) is not None
            }
            if values:
                await self.session.execute(
                    TermModel.__table__.update()
                    .where(TermModel.id == u["id"])
                    .values(**values)
                )
        await self.session.commit()

    async def bulk_add(
        self, domain_id: UUID, items: list[tuple[str, str, int, int]]
    ) -> tuple[int, int]:
        added, skipped = 0, 0
        for word, definition, frequency, star_level in items:
            exists = (
                await self.session.execute(
                    select(TermModel).where(
                        TermModel.domain_id == domain_id,
                        func.lower(TermModel.word) == word.lower(),
                    )
                )
            ).scalar_one_or_none()
            if exists:
                skipped += 1
                continue
            self.session.add(
                TermModel(
                    domain_id=domain_id,
                    word=word,
                    definition=definition or None,
                    frequency=frequency,
                    star_level=star_level,
                )
            )
            added += 1
        await self.session.commit()
        return added, skipped

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
        values = {}
        if definition is not None:
            values["definition"] = definition
        if audio_hash is not None:
            values["audio_hash"] = audio_hash
        if star_level is not None:
            values["star_level"] = star_level
        if image_paths is not None:
            values["image_paths"] = image_paths
        if is_active is not None:
            values["is_active"] = is_active
        if frequency is not None:
            values["frequency"] = frequency
        if values:
            await self.session.execute(
                TermModel.__table__.update()
                .where(TermModel.id == term_id)
                .values(**values)
            )
            await self.session.commit()


class SqlSentenceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, domain_id: UUID, content_en: str) -> Sentence:
        row = (
            await self.session.execute(
                select(SentenceModel).where(SentenceModel.content_en == content_en)
            )
        ).scalar_one_or_none()
        if row:
            return Sentence.model_validate(row)
        obj = SentenceModel(domain_id=domain_id, content_en=content_en)
        self.session.add(obj)
        await self.session.commit()
        await self.session.refresh(obj)
        return Sentence.model_validate(obj)

    async def list_by_domain(self, domain_id: UUID) -> list[Sentence]:
        rows = (
            await self.session.execute(select(SentenceModel).where(SentenceModel.domain_id == domain_id))
        ).scalars().all()
        return [Sentence.model_validate(r) for r in rows]

    async def bulk_add(self, domain_id: UUID, sentences: list[str]) -> tuple[int, int]:
        added, skipped = 0, 0
        for content in sentences:
            exists = (
                await self.session.execute(
                    select(SentenceModel).where(SentenceModel.content_en == content)
                )
            ).scalar_one_or_none()
            if exists:
                skipped += 1
                continue
            self.session.add(SentenceModel(domain_id=domain_id, content_en=content))
            added += 1
        await self.session.commit()
        return added, skipped

    async def update(
        self, sentence_id: UUID, content_cn: str | None = None, audio_hash: str | None = None
    ) -> None:
        values = {}
        if content_cn is not None:
            values["content_cn"] = content_cn
        if audio_hash is not None:
            values["audio_hash"] = audio_hash
        if values:
            await self.session.execute(
                SentenceModel.__table__.update()
                .where(SentenceModel.id == sentence_id)
                .values(**values)
            )
            await self.session.commit()

    async def search_by_text(self, domain_id: UUID, term_text: str) -> list[Sentence]:
        rows = (
            await self.session.execute(
                select(SentenceModel).where(
                    SentenceModel.domain_id == domain_id,
                    SentenceModel.content_en.ilike(f"%{term_text}%"),
                )
            )
        ).scalars().all()
        return [Sentence.model_validate(r) for r in rows]

    async def set_embedding(self, sentence_id: UUID, embedding: list[float]) -> None:
        await self.session.execute(
            SentenceModel.__table__.update()
            .where(SentenceModel.id == sentence_id)
            .values(embedding=embedding)
        )
        await self.session.commit()

    async def search_semantic(
        self, domain_id: UUID, query_embedding: list[float], top_k: int = 10
    ) -> list[dict]:
        rows = (
            await self.session.execute(
                select(
                    SentenceModel,
                    (1 - SentenceModel.embedding.cosine_distance(query_embedding)).label("score"),
                )
                .where(
                    SentenceModel.domain_id == domain_id,
                    SentenceModel.embedding.is_not(None),
                )
                .order_by(SentenceModel.embedding.cosine_distance(query_embedding))
                .limit(top_k)
            )
        ).all()
        result = []
        for sentence, score in rows:
            item = Sentence.model_validate(sentence).model_dump()
            item["score"] = round(float(score), 4)
            result.append(item)
        return result


class SqlMatchRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, term_id: UUID, sentence_id: UUID, cn_explanation: str | None = None) -> None:
        row = (
            await self.session.execute(
                select(MatchModel).where(
                    MatchModel.term_id == term_id, MatchModel.sentence_id == sentence_id
                )
            )
        ).scalar_one_or_none()
        if row:
            if cn_explanation is not None:
                row.cn_explanation = cn_explanation
        else:
            self.session.add(
                MatchModel(term_id=term_id, sentence_id=sentence_id, cn_explanation=cn_explanation)
            )
        await self.session.commit()

    async def list_for_term(self, term_id: UUID) -> list[dict]:
        rows = (
            await self.session.execute(
                select(SentenceModel, MatchModel.cn_explanation)
                .join(MatchModel, MatchModel.sentence_id == SentenceModel.id)
                .where(MatchModel.term_id == term_id)
            )
        ).all()
        result = []
        for sentence, explanation in rows:
            item = Sentence.model_validate(sentence).model_dump()
            item["cn_explanation"] = explanation
            result.append(item)
        return result
