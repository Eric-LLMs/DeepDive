"""Database engine + SQLAlchemy 2.0 async ORM models.

Uses the asyncpg async driver;
see docs/architecture.md for table/field names. This first lands the core learning-domain tables;
materials/chunks/users/conversations etc. are added later at the Agent/RAG layer.
"""
import uuid
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from pgvector.sqlalchemy import Vector

from core.config import settings

# ``pool_pre_ping`` discards connections killed mid-transaction (e.g. a DB commit in
# flight when the turn is cancelled leaves the asyncpg socket broken); without it the
# next checkout can hand that dead connection to a request and fail with
# "connection is closed". Pre-ping trades one round-trip for never serving a dead socket.
engine = create_async_engine(settings.database_url, echo=False, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    # Eagerly fetch server-computed defaults (``server_default``/``onupdate`` like the
    # ``updated_at = now()`` columns) via RETURNING, so committed instances keep their
    # values and stay readable after the session closes instead of raising
    # DetachedInstanceError. Inherited by every mapped subclass.
    __mapper_args__ = {"eager_defaults": True}


class DomainModel(Base):
    """A vocabulary domain. ``user_id`` NULL = public/shared; otherwise private to the owner."""

    __tablename__ = "domains"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    name: Mapped[str] = mapped_column(String)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
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
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
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
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    origin_source: Mapped[str | None] = mapped_column(String)
    content_en: Mapped[str] = mapped_column(Text)
    content_cn: Mapped[str | None] = mapped_column(Text)
    audio_hash: Mapped[str | None] = mapped_column(String)
    cn_explanation: Mapped[str | None] = mapped_column(Text)
    embedding: Mapped[list] = mapped_column(Vector(settings.embedding_dim), nullable=True)


class ArticleModel(Base):
    """A Learning-Platform article: free-text study material importable into the query repo."""

    __tablename__ = "articles"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    domain_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("domains.id", ondelete="SET NULL")
    )
    title: Mapped[str] = mapped_column(String, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


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


class GlobalObjectModel(Base):
    """One physical file per unique SHA-256, shared across all users (dedup / instant upload)."""

    __tablename__ = "global_objects"

    sha256: Mapped[str] = mapped_column(String, primary_key=True)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    storage_key: Mapped[str] = mapped_column(String, nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String)
    ref_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class WorkspaceModel(Base):
    """User-owned group; membership (WorkspaceMemberModel) is the sharing mechanism."""

    __tablename__ = "workspaces"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class WorkspaceMemberModel(Base):
    __tablename__ = "workspace_members"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(String, nullable=False)  # 'owner' | 'editor' | 'viewer'
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class WorkspaceActivityModel(Base):
    """Audit trail of drive mutations (file/folder/member/workspace changes).

    Columns are denormalized and have NO foreign keys on purpose: an entry must survive
    the deletion of the workspace, actor, or target it references. ``actor_username`` and
    ``target_name`` are stored so entries stay readable and searchable after the rows they
    describe are gone.
    """

    __tablename__ = "workspace_activity"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))  # NULL = My Drive
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))  # NULL = system
    actor_username: Mapped[str | None] = mapped_column(String)
    action: Mapped[str] = mapped_column(String, nullable=False)
    target_type: Mapped[str] = mapped_column(String, nullable=False)  # file | folder | member | workspace
    target_id: Mapped[str | None] = mapped_column(String)
    target_name: Mapped[str | None] = mapped_column(String)
    detail: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class FolderModel(Base):
    """A folder within a workspace (or the personal drive when ``workspace_id`` NULL).

    One row per folder path inside a scope; ``path`` is the full '/'-separated
    relative path (e.g. ``"English/Vocab"``), so ancestors are implicit and no
    parent FK is needed. ``user_id`` records the creator; in a shared workspace the
    folder is visible to every member.
    """

    __tablename__ = "folders"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE")
    )
    path: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AssetModel(Base):
    """A logical file owned by a user, pointing at a physical GlobalObjectModel.

    ``file_status`` tracks upload/processing state; ``rag_status`` tracks the async
    parse→chunk→embed pipeline. Deleting an asset only soft-deletes this row (and
    decrements the object's ref_count); the physical bytes live until ref_count hits 0.
    """

    __tablename__ = "assets"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id")
    )
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("workspaces.id")
    )
    domain_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("domains.id", ondelete="SET NULL")
    )
    object_sha256: Mapped[str | None] = mapped_column(
        String, ForeignKey("global_objects.sha256")  # set once the upload completes
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    folder_path: Mapped[str | None] = mapped_column(String)
    mime_type: Mapped[str | None] = mapped_column(String)
    size: Mapped[int | None] = mapped_column(BigInteger)
    file_status: Mapped[str] = mapped_column(String, nullable=False, default="uploading")
    rag_status: Mapped[str] = mapped_column(String, nullable=False, default="not_started")
    meta: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AssetAclModel(Base):
    """Asset-level sharing. ``grantee_user_id`` NULL = public link (anyone with access)."""

    __tablename__ = "asset_acl"
    # A surrogate id PK keeps grantee_user_id nullable (a composite PK would force it
    # NOT NULL and reject the NULL public-link rows). Uniqueness is enforced by the
    # two partial indexes below, matching the original DDL.
    __table_args__ = (
        Index(
            "asset_acl_public_uniq",
            "asset_id",
            unique=True,
            postgresql_where=text("grantee_user_id IS NULL"),
        ),
        Index(
            "asset_acl_grantee_uniq",
            "asset_id",
            "grantee_user_id",
            unique=True,
            postgresql_where=text("grantee_user_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("assets.id", ondelete="CASCADE")
    )
    grantee_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    permission: Mapped[str] = mapped_column(String, nullable=False)  # 'read' | 'write'
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class UploadSessionModel(Base):
    """Chunked upload state: which chunks are received, so a client can resume."""

    __tablename__ = "upload_sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("assets.id", ondelete="CASCADE")
    )
    sha256: Mapped[str] = mapped_column(String, nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    chunk_size: Mapped[int] = mapped_column(Integer, nullable=False)
    num_chunks: Mapped[int] = mapped_column(Integer, nullable=False)
    received_chunks: Mapped[list] = mapped_column(JSONB, default=list)  # bool array
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ChunkModel(Base):
    """RAG chunk in the unified query repository.

    ``source_type`` discriminates the three import paths: ``file`` (a drive asset, the
    legacy path), ``learning`` (sentences / articles) and ``chat`` (Q&A pairs). Only file
    chunks carry ``asset_id``; the others carry ``source_id`` plus owner in ``user_id``.
    ``chunk_kind`` is ``leaf`` (recalled) or ``parent`` (small-to-big context). A leaf
    records its ``parent_chunk_id``; recall searches leaf chunks only and parent_expand
    swaps a leaf hit for its parent's text. ``content_search`` holds jieba-segmented
    text for the CJK keyword channel (English still uses ``content_en`` tsvector).
    """

    __tablename__ = "chunks"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    asset_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("assets.id", ondelete="CASCADE")
    )
    source_type: Mapped[str] = mapped_column(String, nullable=False, default="file")
    source_id: Mapped[str | None] = mapped_column(String)
    user_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    content_en: Mapped[str] = mapped_column(Text, nullable=False)
    content_cn: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[dict] = mapped_column(JSONB, default=dict)  # timestamps/page numbers etc.
    embedding: Mapped[list] = mapped_column(Vector(settings.embedding_dim))
    parent_chunk_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("chunks.id", ondelete="SET NULL")
    )
    chunk_kind: Mapped[str] = mapped_column(String, nullable=False, default="leaf")
    content_search: Mapped[str | None] = mapped_column(Text)


class UserModel(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    username: Mapped[str | None] = mapped_column(String, unique=True)
    password_hash: Mapped[str | None] = mapped_column(String)
    display_name: Mapped[str | None] = mapped_column(String)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    role_id: Mapped[str] = mapped_column(
        String, ForeignKey("user_roles.role_id"), default="regular"
    )
    email: Mapped[str | None] = mapped_column(String)          # unique partial index
    phone: Mapped[str | None] = mapped_column(String)
    avatar: Mapped[str | None] = mapped_column(String)         # /avatars/<user_id>.<ext>
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    meta: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=func.now()
    )


class UserRoleModel(Base):
    """A quota/feature tier: daily+monthly request limits, token limits, and model gating."""

    __tablename__ = "user_roles"

    role_id: Mapped[str] = mapped_column(String, primary_key=True)
    role_name: Mapped[str] = mapped_column(String, nullable=False)
    daily_request_limit: Mapped[int] = mapped_column(Integer, default=50)
    monthly_request_limit: Mapped[int] = mapped_column(Integer, default=1500)
    daily_token_limit: Mapped[int] = mapped_column(BigInteger, default=-1)
    rpm_limit: Mapped[int] = mapped_column(Integer, default=-1)
    monthly_cost_limit: Mapped[float] = mapped_column(Numeric(12, 6), default=-1)
    default_model: Mapped[str] = mapped_column(String, default="")
    models: Mapped[list] = mapped_column(ARRAY(Text), default=list)
    features: Mapped[dict] = mapped_column(JSONB, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class LoginTokenModel(Base):
    """An opaque login/API credential: sha256 hash stored, raw value returned once.

    ``role`` is the principal kind (``admin`` — unlimited, or ``user``); ``role_id`` is an
    optional quota-role override for user tokens (falls back to the owner's role when null).
    ``credential_id`` is the LLM channel pinned to this login. ``is_active`` is the
    login-credential validity only — key grants live in ``access_tokens``.
    """

    __tablename__ = "login_tokens"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    token_hash: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    role: Mapped[str] = mapped_column(String, nullable=False, default="user")
    role_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("user_roles.role_id", ondelete="SET NULL")
    )
    credential_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("llm_credentials.id", ondelete="SET NULL")
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AccessTokenModel(Base):
    """Per-user LLM-key grant: "this user may use this LLM key".

    ``is_active`` off = the user is banned from that key (the Tokens-page key switch); it never
    affects login — login-credential validity lives on ``login_tokens``. A row is created lazily
    the first time the key is assigned to the user (at login).
    """

    __tablename__ = "access_tokens"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    credential_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("llm_credentials.id", ondelete="SET NULL")
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class VerificationTokenModel(Base):
    """One-time email verification / password-reset token (hash stored, raw shown once)."""

    __tablename__ = "verification_tokens"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(String, nullable=False)  # 'verify' | 'reset'
    token_hash: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class UserUsageCounterModel(Base):
    """O(1) quota accounting: one row per (user, period). Atomic UPSERT on each call."""

    __tablename__ = "user_usage_counters"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    period_type: Mapped[str] = mapped_column(String, primary_key=True)  # 'day' | 'month'
    period_start: Mapped[date] = mapped_column(Date, primary_key=True)
    request_count: Mapped[int] = mapped_column(BigInteger, default=0)
    token_count: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class UserUsageLogModel(Base):
    """Append-only per-request usage detail (audit); aggregates live in the counters table."""

    __tablename__ = "user_usage_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    token_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("login_tokens.id", ondelete="SET NULL")
    )
    role_id: Mapped[str | None] = mapped_column(String)
    credential_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("llm_credentials.id", ondelete="SET NULL")
    )
    model_name: Mapped[str | None] = mapped_column(String)
    tool: Mapped[str | None] = mapped_column(String)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Numeric(12, 6), default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AppSettingModel(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class LLMCredentialModel(Base):
    """A provider API credential (base_url + api_key), maintained by the admin."""

    __tablename__ = "llm_credentials"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    base_url: Mapped[str] = mapped_column(String, nullable=False)
    api_key: Mapped[str] = mapped_column(String, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LLMModelModel(Base):
    """Model catalog entry; per-1k-token prices are the PAYG cost source."""

    __tablename__ = "llm_models"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    name: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    provider_model_name: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    prompt_price_per_1k: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), default=Decimal("0")
    )
    completion_price_per_1k: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), default=Decimal("0")
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class CredentialModelModel(Base):
    """N:M routing between a credential and a model (route note + per-key price override)."""

    __tablename__ = "credential_models"

    credential_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("llm_credentials.id", ondelete="CASCADE"),
        primary_key=True,
    )
    model_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("llm_models.id", ondelete="CASCADE"),
        primary_key=True,
    )
    note: Mapped[str | None] = mapped_column(Text)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    weight: Mapped[int] = mapped_column(Integer, default=1)
    prompt_price_per_1k: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    completion_price_per_1k: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class RoleCredentialModel(Base):
    """N:M binding of a role to the LLM channels (llm_credentials) it may use.

    This is the routing source: a role's channel set decides which provider key its
    users are pinned to at login (one channel is randomly picked and stored on the
    access token). ``user_roles.models`` is a legacy display-only field.
    """

    __tablename__ = "role_credentials"

    role_id: Mapped[str] = mapped_column(
        String, ForeignKey("user_roles.role_id", ondelete="CASCADE"), primary_key=True
    )
    credential_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("llm_credentials.id", ondelete="CASCADE"),
        primary_key=True,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class UserWalletModel(Base):
    """Cash wallet: one row per user; balance is authoritative for PAYG deduction."""

    __tablename__ = "user_wallets"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    balance: Mapped[Decimal] = mapped_column(Numeric(14, 6), default=Decimal("0"))
    currency: Mapped[str] = mapped_column(String, default="USD")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class WalletTransactionModel(Base):
    """Append-only wallet ledger; balance_after is a snapshot, never recomputed."""

    __tablename__ = "wallet_transactions"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    type: Mapped[str] = mapped_column(String, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 6), nullable=False)
    balance_after: Mapped[Decimal] = mapped_column(Numeric(14, 6), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[dict] = mapped_column(JSONB, default=dict)
    idempotency_key: Mapped[str | None] = mapped_column(String, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SessionModel(Base):
    """A chat session: created on first message, closed (summarized) on session end."""

    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    summary: Mapped[str | None] = mapped_column(Text)
    # Short auto-generated title (LLM from the first user message); None until finalized.
    title: Mapped[str | None] = mapped_column(Text)


class MessageModel(Base):
    """One chat message: text stored hot, embedding backfilled on session close."""

    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE")
    )
    role: Mapped[str] = mapped_column(String, nullable=False)  # 'user' | 'assistant' | 'tool'
    text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list] = mapped_column(Vector(settings.embedding_dim), nullable=True)
    # True once this message's content is in the RAG query repository. The message row is the
    # source of truth for the client's "✓ Imported" state (survives deletes / regrouping, so a
    # pair's state never spreads to siblings and an imported pair can't be re-imported).
    imported_rag: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SessionEventModel(Base):
    """Append-only session event log (audit/resume), mirroring the in-memory SessionLog."""

    __tablename__ = "session_events"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE")
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    type: Mapped[str] = mapped_column(String, nullable=False)
    timestamp: Mapped[float] = mapped_column(Float, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)


class JobModel(Base):
    """Async task state: the single source of truth for gateway → worker jobs."""

    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    type: Mapped[str] = mapped_column(String, nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(String, nullable=False, default="queued")
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    result: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RagFeedbackModel(Base):
    """RAG retrieval feedback: query → chunks → rating → reason (golden eval data).

    The desktop workbench lets a user rate the chunks behind an answer (👍/👎); each row
    snapshots the query, the retrieved hits (ids + scores + text), and the rating/reason so
    the corpus becomes a golden dataset for future fine-tuning / eval without re-running
    retrieval.
    """

    __tablename__ = "rag_feedback"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    query: Mapped[str] = mapped_column(Text, nullable=False)
    rating: Mapped[bool] = mapped_column(Boolean, nullable=False)  # True = relevant
    reason: Mapped[str | None] = mapped_column(Text)
    hits: Mapped[list] = mapped_column(JSONB, default=list)   # [{id, score, text?}]
    filters: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


_REPO_ROOT = Path(__file__).resolve().parents[3]
_MIGRATIONS_DIR = _REPO_ROOT / "migrations"


def _asyncpg_dsn(database_url: str) -> str:
    """asyncpg connects with a plain ``postgresql://`` DSN, not SQLAlchemy's ``+asyncpg``."""
    return database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def init_db() -> None:
    """Apply pending SQL migrations in order (replaces Alembic).

    Each ``migrations/NNNN_*.sql`` file runs once, inside a transaction; applied versions are
    recorded in ``schema_migrations`` so re-runs are no-ops.
    """
    import asyncpg

    conn = await asyncpg.connect(_asyncpg_dsn(settings.database_url))
    try:
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version TEXT PRIMARY KEY,"
            "name TEXT NOT NULL,"
            "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        applied = {
            row["version"] for row in await conn.fetch("SELECT version FROM schema_migrations")
        }
        for path in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            version = path.stem
            if version in applied:
                continue
            sql = path.read_text(encoding="utf-8")
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute(
                    "INSERT INTO schema_migrations (version, name) VALUES ($1, $2)",
                    version,
                    path.name,
                )
    finally:
        await conn.close()
