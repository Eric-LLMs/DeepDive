"""API request/response models."""
from datetime import datetime
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
    user_id: UUID | None = None      # optional: per-user memory isolation
    session_id: UUID | None = None   # optional: resume an existing session


class MediaGenerateRequest(BaseModel):
    video_path: str
    subtitle_path: str | None = None
    format: str = "pptx"             # "pptx" | "pdf"
    title: str | None = None


class ConfigUpdateRequest(BaseModel):
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str | None = None
    web_search_provider: str | None = None
    web_search_api_key: str | None = None


class LLMProviderModel(BaseModel):
    """One OpenAI-compatible provider card (id/name/endpoint/key/model catalog)."""

    id: str
    name: str = ""
    base_url: str = ""
    api_key: str = ""          # empty string means "keep the existing stored key"
    models: list[str] = []
    model: str = ""


class ProvidersUpdateRequest(BaseModel):
    """Full provider-card list + active selection, written wholesale by the settings UI."""

    providers: list[LLMProviderModel] = []
    active_provider: str = ""
    web_search_provider: str | None = None
    web_search_api_key: str | None = None


class AdminLoginRequest(BaseModel):
    username: str
    password: str


class UserLoginRequest(BaseModel):
    username: str
    password: str


class UserCreateRequest(BaseModel):
    username: str
    password: str
    display_name: str | None = None
    role_id: str = "regular"        # "regular" | "pro" | "vip" | "admin"


class UserUpdateRequest(BaseModel):
    display_name: str | None = None
    role_id: str | None = None
    is_active: bool | None = None
    password: str | None = None


class TokenCreateRequest(BaseModel):
    name: str
    role: str = "user"              # "admin" | "user"
    user_id: UUID | None = None     # required for role="user"
    role_id: str | None = None      # optional quota-role override
    expires_at: datetime | None = None


class TokenUpdateRequest(BaseModel):
    name: str | None = None
    is_active: bool | None = None
    expires_at: datetime | None = None


class GrantUpdateRequest(BaseModel):
    """Flip the per-user LLM-key grant switch (ban / restore a key for a user)."""

    is_active: bool


class RoleUpdateRequest(BaseModel):
    role_name: str | None = None
    daily_request_limit: int | None = None
    monthly_request_limit: int | None = None
    daily_token_limit: int | None = None
    rpm_limit: int | None = None
    monthly_cost_limit: float | None = None
    default_model: str | None = None
    models: list[str] | None = None
    features: dict | None = None
    is_active: bool | None = None


class ModelCreateRequest(BaseModel):
    name: str
    description: str | None = None
    prompt_price_per_1k: float = 0.0
    completion_price_per_1k: float = 0.0
    is_active: bool = True


class ModelUpdateRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    prompt_price_per_1k: float | None = None
    completion_price_per_1k: float | None = None
    is_active: bool | None = None


class CredentialCreateRequest(BaseModel):
    name: str
    base_url: str
    api_key: str
    is_active: bool = True


class CredentialUpdateRequest(BaseModel):
    name: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    is_active: bool | None = None


class WalletTopupRequest(BaseModel):
    user_id: UUID
    amount: float
    description: str = ""


class RoleCreateRequest(BaseModel):
    """Create a brand-new quota/feature role (mirrors UserRoleModel columns)."""

    role_id: str
    role_name: str = ""
    daily_request_limit: int = 50
    monthly_request_limit: int = 1500
    daily_token_limit: int = -1
    rpm_limit: int = -1
    monthly_cost_limit: float = -1.0
    default_model: str = ""
    models: list[str] = []
    features: dict = {}
    is_active: bool = True


class RoleCredentialsUpdateRequest(BaseModel):
    """Wholesale-replace a role's channel bindings (role ↔ llm_credentials, via PK)."""

    credential_ids: list[UUID] = []


class RouteUpsertRequest(BaseModel):
    """Upsert one credential↔model route (composite PK credential_id+model_id).

    ``prompt_price_per_1k`` / ``completion_price_per_1k`` override the catalog price
    for this channel when set; ``None`` means "inherit the catalog price".
    """

    credential_id: UUID
    model_id: UUID
    actual_model_name: str
    priority: int = 0
    weight: int = 1
    prompt_price_per_1k: float | None = None
    completion_price_per_1k: float | None = None
    is_active: bool = True
