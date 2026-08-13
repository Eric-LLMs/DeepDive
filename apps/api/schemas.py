"""API request/response models."""
from uuid import UUID

from pydantic import BaseModel


class DomainCreate(BaseModel):
    name: str


class TermCreate(BaseModel):
    domain_id: UUID
    word: str
    definition: str = ""


class SentenceCreate(BaseModel):
    domain_id: UUID
    content_en: str


class SentenceUpdate(BaseModel):
    sentence_id: UUID
    content_cn: str | None = None
    audio_hash: str | None = None


class TermUpdate(BaseModel):
    term_id: UUID
    definition: str | None = None
    audio_hash: str | None = None
    star_level: int | None = None
    image_paths: list[str] | None = None
    is_active: bool | None = None
    frequency: int | None = None


class ImportRequest(BaseModel):
    domain_id: UUID
    text: str


class TermImportItem(BaseModel):
    word: str
    definition: str = ""
    frequency: int = 1
    star_level: int = 1


class TermImportRequest(BaseModel):
    domain_id: UUID
    items: list[TermImportItem]


class SentenceImportRequest(BaseModel):
    domain_id: UUID
    items: list[str]


class ImageFetchRequest(BaseModel):
    word: str
    definition: str = ""
    context: str = ""
    regenerate: bool = False


class BulkTermUpdate(BaseModel):
    term_id: UUID
    word: str | None = None
    definition: str | None = None
    star_level: int | None = None
    is_active: bool | None = None
    frequency: int | None = None


class BulkUpdateRequest(BaseModel):
    updates: list[BulkTermUpdate]


class MatchCreate(BaseModel):
    term_id: UUID
    sentence_id: UUID
    explanation: str | None = None


class TTSRequest(BaseModel):
    text: str


class GenerateDefinitionRequest(BaseModel):
    term: str


class SyntaxAnalysisRequest(BaseModel):
    sentence: str


class ExplainRequest(BaseModel):
    term: str
    context: str


class ChatRequest(BaseModel):
    message: str
    history: list[dict] = []
