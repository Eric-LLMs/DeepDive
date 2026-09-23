"""API request/response models."""
from datetime import datetime
from typing import Literal
from uuid import UUID

from core.config import settings
from pydantic import BaseModel, model_validator


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


class ChatTurnMessage(BaseModel):
    """One client-held tail entry (session-memory v2).

    ``message_id`` is the id the server handed back when the row was persisted
    (done frame / detail / reconcile). On NORMAL turns it is an OPAQUE label — the
    client already controls what text its own context contains, session ownership is
    pinned by auth, and id authenticity is checked on the compaction/reconcile
    paths where SQL is read anyway. ``null`` = a local-only row never persisted.
    """

    message_id: UUID | None = None
    role: Literal["user", "assistant"]
    content: str


class ChatContextState(BaseModel):
    """The client's Live State watermark (session-memory v2).

    ``summary`` is the structured compaction summary the client holds; it covers
    every model-facing message from the session head through ``through_message_id``
    INCLUSIVE. Presence of this object marks a v2 client: the server then assembles
    the prompt ZERO-READ from ``summary + tail + message`` and never loads history
    from SQL (§8.3: with context_state present an invalid empty tail is a 422,
    never a silent SQL fallback). ``has_pending_mutations`` = the client still has
    unsent Edit/Delete backlog — compaction is deferred while it is true.
    """

    summary: str | None = None
    through_message_id: UUID | None = None
    has_pending_mutations: bool = False


class ViewerSelection(BaseModel):
    """One explicit user action (P0 context): selected text, an image/PDF region, or a
    captured video frame. ``kind="text"`` carries the verbatim selection; ``roi`` /
    ``frame`` describe a visual region (locator-only until an OCR pipeline exists) and
    may reference an already-uploaded screenshot asset via ``image_asset_id``."""

    kind: Literal["text", "roi", "frame"]
    text: str | None = None
    locator: dict | None = None        # {page:n} | {t_ms:x} | {x,y,w,h,image_w,image_h}
    image_asset_id: UUID | None = None


class ViewerPayload(BaseModel):
    """The Viewer's frozen state at click-Send (Viewer Context Provider).

    Pure data the client extracted from what it already has on screen (current page text,
    parsed subtitle cues, selection strings) — the server NEVER retrieves viewer content via
    RAG and never sees the client's file beyond these fields. ``mode`` is the *client
    request*; the server may downgrade it (local words in the message force FOCUS, see
    :func:`api.viewer_context.classify_viewer_mode`). ``viewer=None`` on the request keeps
    the whole legacy chat path byte-identical.
    """

    # ``follow`` separates viewport tracking from P0: closing the focus chip ([x]) sets
    # follow=false but must NOT discard pending selections — a NONE+P0 turn still ships.
    # ``mode`` is the client's explicit statement when known ("none" after the chip is
    # closed); ``None`` = server-side intent classification decides (see classify_viewer_mode).
    follow: bool = True
    mode: Literal["none", "focus", "full"] | None = None
    provenance: Literal["cloud", "local"] = "local"
    asset_id: UUID | None = None       # cloud binding; identity/citation metadata only, never a retrieval key
    name: str
    kind: str = "text"                 # pdf|video|image|text|markdown|office
    page: int | None = None
    t_ms: int | None = None
    focus_text: str | None = None      # current-page text (documents) — FOCUS payload
    cues: list[dict] | None = None    # video: [{start_ms,end_ms,text}] full list; the server computes the window
    full_text: str | None = None      # full document / full subtitles, only when it fits the caps below
    full_chars: int | None = None     # true full length (set even when full_text is omitted for being too large)
    full_trusted: bool = False         # client CONFIRMED it captured every page/cue — never fake partial DOM as full
    selections: list[ViewerSelection] = []

    @model_validator(mode="after")
    def _guard_viewer_caps(self) -> "ViewerPayload":
        if len(self.name) > 512:
            raise ValueError("viewer.name too long")
        if len(self.selections) > 8:
            raise ValueError("too many viewer selections (max 8)")
        for s in self.selections:
            if s.text and len(s.text) > 4000:
                raise ValueError("viewer selection text too long (max 4000 chars)")
            if s.kind == "text" and not (s.text or "").strip():
                raise ValueError("text selection requires non-empty text")
        if self.focus_text and len(self.focus_text) > 12000:
            raise ValueError("viewer.focus_text too long (max 12000 chars)")
        if self.page is not None and not 1 <= self.page <= 10000:
            raise ValueError("viewer.page out of range")
        if self.t_ms is not None and self.t_ms < 0:
            raise ValueError("viewer.t_ms must be >= 0")
        if self.cues is not None:
            if len(self.cues) > 300:
                raise ValueError("too many subtitle cues (max 300)")
            for c in self.cues:
                if not (
                    isinstance(c.get("start_ms"), (int, float))
                    and isinstance(c.get("end_ms"), (int, float))
                    and isinstance(c.get("text"), str)
                    and c["end_ms"] >= c["start_ms"] >= 0
                    and len(c["text"]) <= 500
                ):
                    raise ValueError("invalid subtitle cue (need start_ms<=end_ms, text<=500)")
        if self.full_text is not None and len(self.full_text) > 100_000:
            raise ValueError("viewer.full_text exceeds the 100k-char transport cap")
        return self


class ChatRequest(BaseModel):
    message: str
    context_state: ChatContextState | None = None  # None = legacy client → recovery-mode
    tail: list[ChatTurnMessage] = []               #   bounded SQL load (kept compatible)
    user_id: UUID | None = None      # deprecated: ignored for anonymous requests — the
    guest_token: str | None = None   #   server resolves a guest's identity from the signed
                                     #   gt_ token (api.auth.sign_guest_token), never from a
                                     #   client-supplied user_id.
    session_id: UUID | None = None   # optional: resume an existing session
    ephemeral: bool = False          # Research-tab blank chat (no task selected): a session
                                     #   created for this turn is marked research-type (1), so
                                     #   an unbound throwaway chat never shows in the Sessions
                                     #   list. Ignored when session_id resumes an existing row.
    disable_thinking: bool = False   # live voice-call turns: suppress reasoning tokens for
                                     #   this turn's model calls (faster time-to-first-sentence)
    attach: dict | None = None       # optional: { kind: "asset", asset_id, name } — a cloud
                                     #   file the user wants the agent to troubleshoot; its
                                     #   name + asset_id are prefixed to the message context.
    handoff: dict | None = None      # optional: machine-readable turn context (e.g. the
                                     #   desktop "Resume Research in Chat" button resumes a
                                     #   project with { kind: "research", project_id,
                                     #   mode: "research_resume" }). Formatted into a
                                     #   structured instruction prefix AND sunk into the
                                     #   turn context so tools read it at runtime.
    viewer: ViewerPayload | None = None  # optional: Viewer Context Provider — content the client
                                     #   extracted from the open viewer (current page, subtitle
                                     #   window, full text, explicit selections). Delivered to
                                     #   the LLM as a separate reference-context section; the
                                     #   user's raw query is never modified by it.

    @model_validator(mode="after")
    def _guard_v2_payload(self) -> "ChatRequest":
        if self.context_state is None:
            return self  # legacy client: the router loads SQL in recovery mode
        # §8.3: a v2 client with a real watermark may NOT send an empty tail —
        # that would silently drop the conversation. Only a fresh session
        # (no summary, no watermark) legitimately rides an empty tail.
        if not self.tail and (self.context_state.summary or self.context_state.through_message_id):
            raise ValueError(
                "context_state carries a summary/watermark but tail is empty; "
                "reload the session via GET /sessions/{id} (recovery) instead"
            )
        if len(self.tail) > 2 * settings.history_max_messages:
            raise ValueError("tail exceeds the per-request message cap")
        cap = settings.prompt_max_chars
        if cap and sum(len(t.content) for t in self.tail) > cap:
            raise ValueError("tail exceeds the per-request character budget")
        return self


class ReconcileRequest(BaseModel):
    """Reconnect/recovery alignment: the client Live State is authoritative."""

    context_state: ChatContextState
    tail: list[ChatTurnMessage] = []


class ApprovalResolveRequest(BaseModel):
    allow: bool
    # Optional client feedback surfaced to the model as the tool result when denying
    # (e.g. "user confirmed; a background Cloud Drive job was started instead").
    message: str | None = None


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
    # deck options (slides tool; ignored by the other tools) — see DeckOptions in the deck engine
    count: int | None = None             # target number of CONTENT slides (clamped 3..20)
    audience: str | None = None          # target audience line (cover + Pass B)
    goal: str | None = None              # presentation goal line (cover + Pass B)
    language: str | None = None          # output language directive, e.g. "English" / "中文" (Pass A/B/C)
    format_mode: Literal["detailed", "presenter"] | None = None  # text-density style (Pass B/C)
    # slides engine override: "direct" = one semantic call + local compiler (default
    # via settings.slides_generation_mode); "legacy" = the Brief-chain fallback.
    generation_mode: Literal["direct", "legacy"] | None = None


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


class TestWebSearchRequest(BaseModel):
    """Web-search connectivity probe payload for /config/test-web-search.

    Every field is an optional override over the stored tools config: a blank ``api_key``
    (or None) means "keep the stored one". The result is sanitized — the key is never
    echoed back.
    """

    provider: str | None = None
    api_key: str | None = None
    engine_id: str | None = None
    query: str = ""


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


# ── Intent Registry admin (QIR P1) ───────────────────────────────────────────────

class RegistryDraftCreateRequest(BaseModel):
    """One Draft capability row (the editable Registry source)."""

    capability_id: str
    tool_binding: str
    description: str = ""
    patterns: list[str] = []
    aliases: list[str] = []
    examples: list[str] = []
    negatives: list[str] = []
    arg_slots: dict = {}
    permissions: str = ""
    execution_policy: str = "auto"
    enabled: bool = True
    status: str = "active"
    replacement_capability_id: str | None = None


class RegistryDraftUpdateRequest(BaseModel):
    """Partial edit; ``expected_row_version`` is the optimistic-concurrency token
    the editor read with the row — a stale write is rejected (409), never merged."""

    expected_row_version: int
    patch: dict


class RegistryPublishRequest(BaseModel):
    note: str | None = None


class RegistryRollbackRequest(BaseModel):
    note: str | None = None
