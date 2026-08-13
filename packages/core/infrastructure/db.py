"""Database engine + SQLAlchemy 2.0 async ORM models.

Replaces the old project's SQLite (WAL + timeout). The new architecture uses the asyncpg async driver;
see docs/architecture.md for table/field names. This first lands the core learning-domain tables;
materials/chunks/users/conversations etc. are added later at the Agent/RAG layer.
"""
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from pgvector.sqlalchemy import Vector

from core.config import settings

engine = create_async_engine(settings.database_url, echo=False)
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class DomainModel(Base):
    __tablename__ = "domains"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    name: Mapped[str] = mapped_column(String, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class TermModel(Base):
    __tablename__ = "terms"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    domain_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("domains.id", ondelete="CASCADE")
    )
    word: Mapped[str] = mapped_column(String, nullable=False)
    definition: Mapped[str | None] = mapped_column(Text)
    frequency: Mapped[int] = mapped_column(Integer, default=1)
    star_level: Mapped[int] = mapped_column(Integer, default=1)
    audio_hash: Mapped[str | None] = mapped_column(String)
    image_paths: Mapped[list] = mapped_column(JSONB, default=list)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class SentenceModel(Base):
    __tablename__ = "sentences"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    domain_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("domains.id", ondelete="CASCADE")
    )
    origin_source: Mapped[str | None] = mapped_column(String)
    content_en: Mapped[str] = mapped_column(Text, unique=True)
    content_cn: Mapped[str | None] = mapped_column(Text)
    audio_hash: Mapped[str | None] = mapped_column(String)
    cn_explanation: Mapped[str | None] = mapped_column(Text)
    embedding: Mapped[list] = mapped_column(Vector(settings.embedding_dim), nullable=True)


class MatchModel(Base):
    __tablename__ = "matches"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    term_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("terms.id", ondelete="CASCADE")
    )
    sentence_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("sentences.id", ondelete="CASCADE")
    )
    cn_explanation: Mapped[str | None] = mapped_column(Text)


class MaterialModel(Base):
    """Unified material: video / document / vocabulary domain are all one kind of learning material."""

    __tablename__ = "materials"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    type: Mapped[str] = mapped_column(String, nullable=False)  # 'domain' | 'video' | 'document'
    title: Mapped[str] = mapped_column(String, nullable=False)
    source_url: Mapped[str | None] = mapped_column(String)
    meta: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ChunkModel(Base):
    """Unified text chunk (core abstraction: everything is a text chunk)."""

    __tablename__ = "chunks"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    material_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("materials.id", ondelete="CASCADE")
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    content_en: Mapped[str] = mapped_column(Text, nullable=False)
    content_cn: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[dict] = mapped_column(JSONB, default=dict)  # timestamps/page numbers etc.
    embedding: Mapped[list] = mapped_column(Vector(settings.embedding_dim))


async def init_db() -> None:
    """Create tables + enable the pgvector extension (idempotent)."""
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        await conn.run_sync(Base.metadata.create_all)
        # Dev migration: add the embedding column to an already-existing sentences table.
        await conn.execute(
            text(
                f"ALTER TABLE sentences ADD COLUMN IF NOT EXISTS embedding "
                f"vector({settings.embedding_dim})"
            )
        )
