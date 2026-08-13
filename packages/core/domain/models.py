"""Domain entities.

The new architecture uses UUID primary keys uniformly (the old project used INTEGER auto-increment; UUIDs suit multi-tenant/distributed scenarios better).
Entities are decoupled from the ORM and can be built from ORM objects via pydantic from_attributes.
"""
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class Domain(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID | None = None
    name: str
    created_at: datetime | None = None


class Term(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID | None = None
    domain_id: UUID
    word: str
    definition: str | None = None
    frequency: int = 1
    star_level: int = 1
    audio_hash: str | None = None
    image_paths: list[str] = Field(default_factory=list)
    is_active: bool = True


class Sentence(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID | None = None
    domain_id: UUID
    origin_source: str | None = None
    content_en: str
    content_cn: str | None = None
    audio_hash: str | None = None
    cn_explanation: str | None = None


class Match(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID | None = None
    term_id: UUID
    sentence_id: UUID
    cn_explanation: str | None = None


class Chunk(BaseModel):
    """Unified text chunk abstraction (video timestamp chunks / document page chunks / sentence chunks)."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID | None = None
    material_id: UUID
    seq: int
    content_en: str
    content_cn: str | None = None
    meta: dict = Field(default_factory=dict)
