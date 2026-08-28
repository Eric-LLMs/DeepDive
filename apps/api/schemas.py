"""API request/response models."""
from datetime import datetime
from typing import Literal
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


class SessionRenameRequest(BaseModel):
    title: str | None = None


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
    user_id: UUID | None = None      # deprecated: ignored for anonymous requests — the
    guest_token: str | None = None   #   server resolves a guest's identity from the signed
                                     #   gt_ token (api.auth.sign_guest_token), never from a
                                     #   client-supplied user_id.
    session_id: UUID | None = None   # optional: resume an existing session
    attach: dict | None = None       # optional: { kind: "asset", asset_id, name } — a cloud
                                     #   file the user wants the agent to troubleshoot; its
                                     #   name + asset_id are prefixed to the message context.


class ApprovalResolveRequest(BaseModel):
    allow: bool


class MediaGenerateRequest(BaseModel):
    video_path: str
    subtitle_path: str | None = None
    format: str = "pptx"             # "pptx" | "pdf"
    title: str | None = None


class ToolkitGenerateRequest(BaseModel):
    tool: Literal["slides", "mindmap", "summary"]
    paths: list[str] | None = None       # workspace-relative file paths (file mode)
    output_dir: str | None = None        # workspace-relative output dir override (file mode)
    session_id: UUID | None = None       # generate from this session's conversation (session mode)
    file_ids: list[UUID] | None = None   # generate from these Cloud Drive files (cloud-file mode)
    folder_path: str | None = None       # Cloud Drive target folder (session/cloud mode; None = drive root)
    name: str | None = None              # output file name stem; None = auto-named from the session title / first file
    prompt: str | None = None            # per-task custom prompt appended to the default system prompt


class ConfigUpdateRequest(BaseModel):
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str | None = None
    web_search_provider: str | None = None
    web_search_api_key: str | None = None
    web_search_engine_id: str | None = None


class LLMProviderModel(BaseModel):
    """One OpenAI-compatible provider card (id/name/endpoint/key/model catalog)."""

    id: str
    name: str = ""
    base_url: str = ""
    api_key: str = ""          # empty string means "keep the existing stored key"
    models: list[str] = []
    model: str = ""


class ProbeModelsRequest(BaseModel):
    """Probe payload for /config/probe-models — only the endpoint + key are used."""

    base_url: str
    api_key: str = ""


class SMTPSettings(BaseModel):
    """SMTP server config for account emails (password is masked on GET)."""

    host: str = ""
    port: int = 587
    user: str = ""
    password: str = ""          # empty string means "keep the existing stored password"
    from_email: str = ""
    use_tls: bool = True
    use_ssl: bool = False
    enabled: bool = True


class TestEmailRequest(BaseModel):
    to_email: str


class ProvidersUpdateRequest(BaseModel):
    """Full provider-card list + active selection, written wholesale by the settings UI."""

    providers: list[LLMProviderModel] = []
    active_provider: str = ""
    web_search_provider: str | None = None
    web_search_api_key: str | None = None
    web_search_engine_id: str | None = None
    smtp: SMTPSettings | None = None
    # Generic tool-config namespace: tools.<tool_id>.<param>. Kept in lock-step with the
    # legacy web_search_* / smtp keys (mirrored on save) so older read paths keep working.
    tools: dict[str, dict] | None = None


class AdminLoginRequest(BaseModel):
    username: str
    password: str


class UserLoginRequest(BaseModel):
    username: str
    password: str


class RegisterRequest(BaseModel):
    """Self-service signup; the account is gated on email verification before login."""

    username: str
    email: str
    password: str
    display_name: str | None = None


class ResendVerificationRequest(BaseModel):
    email: str


class ForgotPasswordRequest(BaseModel):
    email: str


class ResetPasswordRequest(BaseModel):
    token: str
    password: str


class ProfileUpdateRequest(BaseModel):
    """Self-service profile edit. ``current_password`` + ``new_password`` only when changing it."""

    display_name: str | None = None
    username: str | None = None
    email: str | None = None
    phone: str | None = None
    current_password: str | None = None
    new_password: str | None = None


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
    email: str | None = None
    phone: str | None = None
    email_verified: bool | None = None


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
    provider_model_name: str | None = None
    description: str | None = None
    prompt_price_per_1k: float = 0.0
    completion_price_per_1k: float = 0.0
    is_active: bool = True


class ModelUpdateRequest(BaseModel):
    name: str | None = None
    provider_model_name: str | None = None
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


class ChatTestRequest(BaseModel):
    """Simulate a PC-chat request for the chosen (user, role, channel) combo."""

    user_id: UUID | None = None       # test account (None = treat as anonymous)
    role_id: str | None = None        # override the role used for routing/model default
    credential_id: UUID | None = None # pin a specific channel (None = auto-resolve)
    message: str = "你好,请简单回复 OK"


class RagConfigUpdateRequest(BaseModel):
    """Wholesale pipeline config blob; validated server-side against the node registry."""

    config: dict = {}


class RagTestRequest(BaseModel):
    """Run the configured pipeline and return the per-node trace."""

    query: str = ""
    top_k: int = 5
    domain_id: str | None = None


class RagChunkPreviewRequest(BaseModel):
    """Preview chunking / enrichment for a text sample without persisting anything."""

    strategy: str = "fixed"
    chunk_chars: int = 1200
    overlap: int = 150
    text: str = ""
    cjk: bool = False
    contextual: bool = False


class RagEvalRequest(BaseModel):
    """Run the golden-set regression and return the metric table."""

    golden_path: str | None = None


class RagFeedbackRequest(BaseModel):
    """Rate the retrieved chunks behind an answer (👍 relevant / 👎 not)."""

    query: str
    rating: bool
    reason: str = ""
    hits: list[dict] = []  # [{id, score, text?}] as returned by retrieval


class LearningImportRequest(BaseModel):
    """Push Learning-Platform content (sentences / articles) into the query repository."""

    kind: str  # "sentence" | "article"
    ids: list[str] = []


class ArticleCreateRequest(BaseModel):
    """Create a Learning-Platform article (optionally imported into the query repo)."""

    title: str
    content: str = ""
    domain_id: str | None = None


class ChatImportRequest(BaseModel):
    """Import one chat Q&A pair (a user message + its assistant reply) as a repo chunk."""

    session_id: str
    user_message_id: str
    assistant_message_id: str


class ChatSessionImportRequest(BaseModel):
    """Import a whole chat session: LLM-groups the Q&A turns into query-repo chunks."""

    session_id: str


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

    ``note`` is a free-text route purpose ("what this route is for"). The model actually
    sent upstream is the catalog entry's ``provider_model_name``, not this note.
    ``prompt_price_per_1k`` / ``completion_price_per_1k`` override the catalog price
    for this channel when set; ``None`` means "inherit the catalog price".
    """

    credential_id: UUID
    model_id: UUID
    note: str | None = None
    priority: int = 0
    weight: int = 1
    prompt_price_per_1k: float | None = None
    completion_price_per_1k: float | None = None
    is_active: bool = True
