"""FastAPI app: expose core use cases as REST/SSE.

Start: uvicorn api.main:app --reload
"""
import json
import logging
import random
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

from arq import create_pool
from arq.connections import RedisSettings
from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

from api.auth import (
    AuthAdmin,
    AuthUser,
    require_admin,
    require_user,
    require_user_optional,
    sign_console_token,
    verify_admin,
)
from api.deps import _embedder, get_agent, get_task_queue, get_vocab_service, llm
from api.schemas import (
    AdminLoginRequest,
    BulkUpdateRequest,
    ChatRequest,
    ChatTestRequest,
    CredentialCreateRequest,
    CredentialUpdateRequest,
    DomainCreate,
    ExplainRequest,
    ForgotPasswordRequest,
    GenerateDefinitionRequest,
    ImageFetchRequest,
    ImportRequest,
    MatchCreate,
    MediaGenerateRequest,
    ModelCreateRequest,
    ModelUpdateRequest,
    ProbeModelsRequest,
    ProfileUpdateRequest,
    ProvidersUpdateRequest,
    RegisterRequest,
    ResendVerificationRequest,
    ResetPasswordRequest,
    RoleCreateRequest,
    RoleCredentialsUpdateRequest,
    RoleUpdateRequest,
    RouteUpsertRequest,
    SentenceCreate,
    SentenceImportRequest,
    SentenceUpdate,
    SyntaxAnalysisRequest,
    TermCreate,
    TermImportRequest,
    TermUpdate,
    TestEmailRequest,
    TTSRequest,
    GrantUpdateRequest,
    TokenCreateRequest,
    TokenUpdateRequest,
    UserCreateRequest,
    UserLoginRequest,
    UserUpdateRequest,
    WalletTopupRequest,
)
from core.config import settings
from core.infrastructure.billing import (
    compute_cost,
    deduct,
    get_balance,
    get_model_prices,
    list_transactions,
    topup,
)
from core.infrastructure.db import (
    AccessTokenModel,
    CredentialModelModel,
    LoginTokenModel,
    LLMCredentialModel,
    LLMModelModel,
    RoleCredentialModel,
    SessionLocal,
    SessionModel,
    UserModel,
    UserRoleModel,
    UserUsageCounterModel,
    UserUsageLogModel,
    UserWalletModel,
    VerificationTokenModel,
    init_db,
)
from core.infrastructure.jobs import (
    ANALYZE_SYNTAX,
    EXPLAIN,
    GENERATE_DEFINITION,
    GENERATE_MEDIA,
    IMAGE_FETCH,
    INDEX_SENTENCES,
    SESSION_FINALIZE,
    TTS,
    TaskQueue,
)
from core.infrastructure.memory import (
    SessionMemoryStore,
    create_session,
    ensure_user,
    list_sessions,
    load_session_messages,
)
from core.infrastructure.mailer import MailNotConfigured, send_email
from core.infrastructure.security import (
    check_quota,
    ensure_admin_user,
    ensure_default_admin,
    generate_token,
    get_role,
    get_setting,
    hash_password,
    hash_token,
    list_roles,
    role_to_dict,
    set_setting,
    verify_password,
)
from collections import defaultdict

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

logger = logging.getLogger("uvicorn.error")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    async with SessionLocal() as session:
        await ensure_default_admin(session)
        await ensure_admin_user(session)
        await _bootstrap_config(session)
    redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    app.state.redis = redis
    yield
    await redis.aclose()


app = FastAPI(title="DeepDive API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # open during dev; tighten for production
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Admin console: a single self-contained HTML page (inline CSS/JS) served by the API.
ADMIN_DIR = Path(__file__).parent / "admin"
ADMIN_INDEX = ADMIN_DIR / "index.html"

# Serve cached TTS audio / images (paths produced by TTS and image scraping).
for _dir in (settings.audio_cache_path, settings.image_cache_path):
    _dir.mkdir(parents=True, exist_ok=True)
app.mount("/audio", StaticFiles(directory=Path(settings.audio_cache_path).resolve()), name="audio")
app.mount("/images", StaticFiles(directory=Path(settings.image_cache_path).resolve()), name="images")

# User avatar uploads (self-service profile).
AVATAR_DIR = Path("data/avatars")
AVATAR_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/avatars", StaticFiles(directory=AVATAR_DIR.resolve()), name="avatars")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


# ── Token minting ──
async def _mint_token(
    *,
    role: str,
    name: str,
    user_id: UUID | None = None,
    role_id: str | None = None,
    credential_id: UUID | None = None,
    expires_at: datetime | None = None,
) -> str:
    """Create a login_tokens row and return the raw token (shown to the client once)."""
    raw, token_hash = generate_token()
    async with SessionLocal() as session:
        session.add(
            LoginTokenModel(
                user_id=user_id,
                name=name,
                token_hash=token_hash,
                role=role,
                role_id=role_id,
                credential_id=credential_id,
                expires_at=expires_at,
            )
        )
        await session.commit()
    return raw


async def _login_token(
    session,
    *,
    user_id: UUID,
    name: str,
    credential_id: UUID | None,
    expires_at: datetime,
) -> str:
    """Return a login credential for (user, channel), reusing the existing row when possible.

    One row per (user, pinned channel): a re-login on the same channel rotates the secret
    and bumps the timestamps instead of inserting a new row, so ``login_tokens`` stops
    growing with every login (enforced by the partial unique indexes on the table).

    This always mints — a disabled key grant never blocks the login. ``_pick_credential``
    has already excluded banned keys, so the channel passed here is always one the user may
    use (or None); a banned key is simply not re-picked.

    When a channel is pinned, its ``access_tokens`` grant row is created **lazily** the
    first time: the per-user key-grant matrix is what the Tokens page toggles, and the ban
    must be sticky (a re-login must not revive it, so an existing disabled grant is left
    alone).
    """
    if credential_id is not None:
        grant = (
            await session.execute(
                select(AccessTokenModel).where(
                    AccessTokenModel.user_id == user_id,
                    AccessTokenModel.credential_id == credential_id,
                )
            )
        ).scalar_one_or_none()
        if grant is None:
            session.add(AccessTokenModel(user_id=user_id, credential_id=credential_id))
    existing = (
        await session.execute(
            select(LoginTokenModel)
            .where(
                LoginTokenModel.user_id == user_id,
                (
                    LoginTokenModel.credential_id.is_(None)
                    if credential_id is None
                    else LoginTokenModel.credential_id == credential_id
                ),
            )
            .order_by(LoginTokenModel.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        raw, token_hash = generate_token()
        existing.token_hash = token_hash
        existing.is_active = True
        existing.last_used_at = datetime.now(timezone.utc)
        existing.expires_at = expires_at
        await session.commit()
        return raw
    raw, token_hash = generate_token()
    session.add(
        LoginTokenModel(
            user_id=user_id,
            name=name,
            token_hash=token_hash,
            role="user",
            role_id=None,
            credential_id=credential_id,
            expires_at=expires_at,
        )
    )
    await session.commit()
    return raw


async def _pick_credential(
    session, role_id: str, user_id: UUID | None = None
) -> UUID | None:
    """Randomly pick one active LLM channel bound to a role (None if none is usable).

    Only bindings whose own ``is_active`` flag and the channel's ``is_active`` are both set
    qualify, so the admin can disable a channel either via its credential row or via the
    per-role binding without touching the other.

    When ``user_id`` is given, channels the user has a *disabled ``access_tokens`` grant* for
    are excluded — the Tokens page bans a user from a specific LLM key by flipping that grant's
    ``is_active``, and the ban must be sticky (a re-login must not revive it).
    """
    rows = (
        await session.execute(
            select(RoleCredentialModel.credential_id)
            .join(
                LLMCredentialModel,
                LLMCredentialModel.id == RoleCredentialModel.credential_id,
            )
            .where(
                RoleCredentialModel.role_id == role_id,
                RoleCredentialModel.is_active.is_(True),
                LLMCredentialModel.is_active.is_(True),
            )
        )
    ).scalars().all()
    candidates = list(rows)
    if user_id is not None and candidates:
        banned = set(
            (
                await session.execute(
                    select(AccessTokenModel.credential_id)
                    .where(
                        AccessTokenModel.user_id == user_id,
                        AccessTokenModel.credential_id.is_not(None),
                        AccessTokenModel.is_active.is_(False),
                    )
                )
            ).scalars().all()
        )
        if banned:
            candidates = [c for c in candidates if c not in banned]
    if not candidates:
        return None
    return random.choice(candidates)


async def _user_banned_from(session, user_id: UUID | None, credential_id: UUID) -> bool:
    """True if the user has a disabled ``access_tokens`` grant for this channel (a Tokens ban)."""
    if user_id is None:
        return False
    row = (
        await session.execute(
            select(AccessTokenModel.id)
            .where(
                AccessTokenModel.user_id == user_id,
                AccessTokenModel.credential_id == credential_id,
                AccessTokenModel.is_active.is_(False),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return row is not None


async def _resolve_chat_route(
    session, token: LoginTokenModel | None, role_id: str
) -> tuple[str, str, str, str, UUID | None]:
    """Resolve ``(base_url, api_key, provider_model, business_name, credential_id)``.

    ``token`` carries the channel pinned at login when present; ``role_id`` is the effective
    role (the user's role, or ``anonymous`` for guests). ``provider_model`` is the real model
    id sent upstream; ``business_name`` is the catalog display name used for billing/usage;
    ``credential_id`` is the serving channel (None when no channel resolved — recorded on the
    usage log so the admin can aggregate cost per channel).

    - A pinned channel that is still active and not banned for the user is used directly;
      the model is the role's ``default_model``, else the channel's preferred active route,
      else the first active catalog model.
    - A pinned channel that was disabled (credential-level, or a user-level Tokens ban on
      that key) fails over to another active channel of the same role the user is not banned
      from.
    - No pinned channel (guest, or admin/legacy token) picks fresh from the role; if the role
      has none, empty strings signal "use the configured global client".
    """
    user_id = token.user_id if token is not None else None
    credential_id = token.credential_id if token is not None else None
    if credential_id is not None:
        if not await _user_banned_from(session, user_id, credential_id):
            credential = await session.get(LLMCredentialModel, credential_id)
            if credential is not None and credential.is_active:
                return await _channel_route(session, credential, role_id)
        alt = await _pick_credential(session, role_id, user_id)
        if alt is not None and alt != credential_id:
            credential = await session.get(LLMCredentialModel, alt)
            if credential is not None:
                return await _channel_route(session, credential, role_id)
        business = await _fallback_model(session, role_id)
        provider = await _provider_model_name(session, business) if business else ""
        return "", "", provider, business, None
    picked = await _pick_credential(session, role_id, user_id)
    if picked is not None:
        credential = await session.get(LLMCredentialModel, picked)
        if credential is not None:
            return await _channel_route(session, credential, role_id)
    return "", "", "", "", None


async def _provider_model_name(session, display_name: str) -> str:
    """Map a catalog display name (or raw id) to the provider's real model id.

    Prefers an exact display-name match, then falls back to the provider id, so a
    ``default_model`` that is already a raw provider id round-trips unchanged and a
    provider id shared by several catalog entries stays unambiguous. Unknown strings
    pass through as-is (backwards compatibility with the legacy config).
    """
    if not display_name:
        return ""
    m = (
        await session.execute(
            select(LLMModelModel).where(LLMModelModel.name == display_name)
        )
    ).scalar_one_or_none()
    if m is None:
        m = (
            await session.execute(
                select(LLMModelModel).where(LLMModelModel.provider_model_name == display_name)
            )
        ).scalar_one_or_none()
    if m is None or not m.provider_model_name:
        return display_name
    return m.provider_model_name


async def _fallback_model(session, role_id: str | None = None) -> str:
    """Catalog display name used as the global default when no channel route resolves.

    Prefers the role's ``default_model`` (a catalog display name or raw provider id),
    else the first active catalog model (created earliest wins, so the default never
    depends on row order). Returns ``""`` when the catalog has no active model.
    """
    if role_id:
        role = await get_role(session, role_id)
        if role is not None and role.default_model:
            return role.default_model
    m = (
        await session.execute(
            select(LLMModelModel)
            .where(LLMModelModel.is_active.is_(True))
            .order_by(LLMModelModel.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()
    return m.name if m is not None else ""


async def _channel_route(
    session, credential: LLMCredentialModel, role_id: str | None
) -> tuple[str, str, str, str, UUID | None]:
    """Return ``(base_url, api_key, provider_model, business_name, credential_id)``.

    The model is resolved to the role's ``default_model`` (a catalog display name) when set,
    else the credential's preferred active route's catalog entry, else the first active
    catalog model. ``provider_model`` is that display name mapped to the provider's real id —
    the name sent upstream; ``business_name`` is the catalog display name, used for billing
    and usage stats. ``credential_id`` is the serving channel (recorded on the usage log).
    A route's ``note`` is a purpose label only.
    """
    business = ""
    if role_id:
        role = await get_role(session, role_id)
        if role is not None and role.default_model:
            business = role.default_model
    if not business:
        model_id = (
            await session.execute(
                select(CredentialModelModel.model_id)
                .where(
                    CredentialModelModel.credential_id == credential.id,
                    CredentialModelModel.is_active.is_(True),
                )
                .order_by(CredentialModelModel.priority)
                .limit(1)
            )
        ).scalar_one_or_none()
        if model_id is not None:
            catalog = await session.get(LLMModelModel, model_id)
            if catalog is not None:
                business = catalog.name
    if not business:
        business = await _fallback_model(session, role_id)
    provider = await _provider_model_name(session, business)
    return credential.base_url, credential.api_key, provider or business, business, credential.id


def _login_expiry() -> datetime:
    """Login-token lifetime, mirroring the old JWT expiry."""
    return datetime.now(timezone.utc) + timedelta(minutes=settings.access_token_expire_minutes)


# ── Admin console ──
@app.post("/admin/login")
async def admin_login(body: AdminLoginRequest) -> dict:
    """Verify the single admin account and return a stateless console session token.

    Console sessions are signed strings held in the browser's localStorage — nothing is
    written to login_tokens, so console logins no longer accumulate duplicate rows.
    Persisted tokens (hashed in login_tokens) are only minted via the Tokens page for
    external API use.
    """
    if not await verify_admin(body.username, body.password):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = await sign_console_token(body.username, _login_expiry())
    return {"access_token": token, "username": body.username}


@app.get("/admin/me")
async def admin_me(admin: AuthAdmin = Depends(require_admin)) -> dict:
    """Return the authenticated admin username (used by the admin page on load)."""
    return {"username": admin.username}


@app.get("/admin")
@app.get("/admin/")
async def admin_page() -> FileResponse:
    """Serve the self-contained admin console."""
    if not ADMIN_INDEX.exists():
        raise HTTPException(status_code=404, detail="admin console not found")
    return FileResponse(ADMIN_INDEX, media_type="text/html")


# ── User auth (desktop workbench login) ──
@app.post("/auth/login")
async def user_login(body: UserLoginRequest) -> dict:
    """Verify a user's credentials and mint an opaque user token.

    Self-registered accounts must complete email verification before they can log in
    (``email IS NOT NULL AND NOT email_verified`` → 403); admin-deactivated accounts are
    rejected too, each with a distinct Chinese hint the clients can surface verbatim.
    """
    async with SessionLocal() as session:
        row = (
            await session.execute(select(UserModel).where(UserModel.username == body.username))
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=401, detail="用户名或密码错误")
        if row.email and not row.email_verified:
            raise HTTPException(
                status_code=403,
                detail="邮箱未验证,请先查收邮件完成验证。未收到?可在个人资料里重新发送。",
            )
        if not row.is_active:
            raise HTTPException(status_code=403, detail="账号已被停用,请联系管理员。")
        if not verify_password(body.password, row.password_hash):
            raise HTTPException(status_code=401, detail="用户名或密码错误")
        role = await get_role(session, row.role_id)
        credential_id = await _pick_credential(session, row.role_id, row.id)
        token = await _login_token(
            session,
            user_id=row.id,
            name=row.username,
            credential_id=credential_id,
            expires_at=_login_expiry(),
        )
    return {
        "access_token": token,
        "username": row.username,
        "display_name": row.display_name,
        "role_id": row.role_id,
        "role_name": role.role_name if role else row.role_id,
    }


@app.get("/auth/me")
async def auth_me(user: AuthUser = Depends(require_user)) -> dict:
    """Return the authenticated user's profile + role quota (desktop account panel)."""
    async with SessionLocal() as session:
        row = (
            await session.execute(select(UserModel).where(UserModel.id == user.user_id))
        ).scalar_one_or_none()
        role = await get_role(session, user.role.role_id)
    return {
        "user_id": str(user.user_id),
        "username": user.username,
        "display_name": user.display_name,
        "email": row.email if row else None,
        "phone": row.phone if row else None,
        "avatar": row.avatar if row else None,
        "email_verified": row.email_verified if row else False,
        "role_id": user.role.role_id,
        "role_name": role.role_name if role else user.role.role_id,
        "quota": role_to_dict(user.role),
    }


@app.get("/auth/usage")
async def auth_usage(
    start: str | None = None,   # ISO date/datetime — logs from this moment (inclusive)
    end: str | None = None,     # ISO date/datetime — logs until this moment (inclusive)
    model: str | None = None,   # fuzzy match on model_name
    limit: int = 20,            # logs per page
    offset: int = 0,
    user: AuthUser = Depends(require_user),
) -> dict:
    """Return the signed-in user's own balance, daily counters, usage logs, and ledger.

    Mirrors the admin ``/admin/users/{id}/usage`` shape, but the user_id is always the
    caller's own — a user can never query anyone else's data.
    """
    async with SessionLocal() as session:
        balance = await get_balance(session, user.user_id)
        wallet = await session.get(UserWalletModel, user.user_id)
        report = await _usage_report(session, user.user_id, start, end, model, limit, offset)
    report["balance"] = float(balance)
    report["currency"] = wallet.currency if wallet else "USD"
    return report


@app.get("/auth/models")
async def auth_models(user: AuthUser = Depends(require_user)) -> dict:
    """Return the model catalog so a user can inspect any model they used in the UI.

    Read-only, no sensitive data — reuses the same masked shape as the admin
    ``/admin/models`` endpoint, so regular users see only display fields + prices.
    """
    async with SessionLocal() as session:
        rows = (
            await session.execute(select(LLMModelModel).order_by(LLMModelModel.created_at))
        ).scalars().all()
    return {"models": [_masked_model(m) for m in rows]}


# ── Registration / email verification / password reset / profile ──

def _html_page(title: str, body: str) -> HTMLResponse:
    """Self-contained Chinese HTML page served by verify/reset email links."""
    html = (
        "<!doctype html><html lang='zh'><head><meta charset='utf-8'>"
        f"<title>{title}</title>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<style>"
        "body{font-family:-apple-system,'Segoe UI',Roboto,'Microsoft YaHei',sans-serif;"
        "background:#f5f7fa;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}"
        ".card{background:#fff;border:1px solid #e3e6ec;border-radius:12px;"
        "box-shadow:0 8px 30px rgba(0,0,0,.06);padding:36px 40px;max-width:420px;width:100%;text-align:center}"
        "h1{font-size:20px;margin:0 0 14px}p{color:#555;font-size:14px;line-height:1.7;margin:0 0 16px;word-break:break-all}"
        "input{width:100%;box-sizing:border-box;padding:10px 12px;margin:4px 0 10px;"
        "border:1px solid #d5dae2;border-radius:8px;font-size:14px}"
        "button{width:100%;padding:10px 12px;border:0;border-radius:8px;background:#2f6fed;color:#fff;font-size:14px;cursor:pointer}"
        "button:hover{background:#2457c8}.err{color:#c0392b;font-size:13px;margin:8px 0 0}.ok{color:#1e8449;font-size:13px}"
        "</style></head><body><div class='card'>" + body + "</div></body></html>"
    )
    return HTMLResponse(content=html)


async def _smtp_config() -> dict:
    return (await _load_config()).get("smtp") or {}


async def _issue_verification(session, user_id: UUID, kind: str, ttl_minutes: int) -> str:
    """Mint a one-time verification/reset token row; returns the raw token (hash stored)."""
    raw, token_hash_ = generate_token()
    session.add(
        VerificationTokenModel(
            user_id=user_id,
            kind=kind,
            token_hash=token_hash_,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes),
        )
    )
    await session.commit()
    return raw


async def _consume_verification(session, raw: str, kind: str) -> VerificationTokenModel | None:
    """Validate (kind, unused, unexpired) a one-time token and mark it used; None otherwise."""
    row = (
        await session.execute(
            select(VerificationTokenModel).where(
                VerificationTokenModel.token_hash == hash_token(raw)
            )
        )
    ).scalar_one_or_none()
    if row is None or row.kind != kind or row.used_at is not None:
        return None
    if row.expires_at is not None and row.expires_at < datetime.now(timezone.utc):
        return None
    row.used_at = datetime.now(timezone.utc)
    return row


async def _send_account_email(subject: str, to: str, base: str, raw_token: str, path: str) -> dict:
    """Send an account email; on failure (or no SMTP) return the raw link as a dev fallback."""
    smtp = await _smtp_config()
    link = f"{base.rstrip('/')}/{path}?token={raw_token}"
    html = (
        "<p>您好,</p><p>请点击下方链接完成操作(链接 24 小时内有效,只能使用一次):</p>"
        f'<p><a href="{link}" style="color:#2f6fed">{link}</a></p>'
        "<p>如果不是您本人操作,请忽略此邮件。</p>"
    )
    try:
        await send_email(smtp, to, subject, html)
        return {}
    except MailNotConfigured:
        logger.warning("SMTP 未配置,邮件链接(仅开发用): %s", link)
        return {"debug_verify_url": link}
    except Exception as err:  # noqa: BLE001 — surface the failure instead of a 500
        logger.error("发送邮件失败(%s): %s", to, err)
        return {"debug_verify_url": link, "email_error": str(err)}


@app.post("/auth/register")
async def register(body: RegisterRequest, request: Request) -> dict:
    """Self-service signup: create a regular account gated on email verification."""
    username = body.username.strip()
    email = body.email.strip().lower()
    if not username or not email or not body.password:
        raise HTTPException(status_code=400, detail="用户名、邮箱和密码不能为空")
    if len(body.password) < 6:
        raise HTTPException(status_code=400, detail="密码至少 6 位")
    async with SessionLocal() as session:
        dup = (
            await session.execute(
                select(UserModel).where(
                    (UserModel.username == username) | (UserModel.email == email)
                )
            )
        ).scalars().first()
        if dup is not None:
            raise HTTPException(status_code=409, detail="用户名或邮箱已被注册")
        row = UserModel(
            username=username,
            email=email,
            password_hash=hash_password(body.password),
            display_name=body.display_name or None,
            role_id="regular",
            is_active=True,
            email_verified=False,
        )
        session.add(row)
        await session.flush()
        raw = await _issue_verification(session, row.id, "verify", 1440)
    base = str(request.base_url).rstrip("/")
    dev = await _send_account_email("DeepDive 邮箱验证", email, base, raw, "auth/verify-email")
    return {"status": "ok", "message": "注册成功,请查收邮件完成邮箱验证。", **dev}


@app.get("/auth/verify-email")
async def verify_email(token: str) -> HTMLResponse:
    """Email-link landing: mark the account verified (one-time token)."""
    async with SessionLocal() as session:
        vrow = await _consume_verification(session, token, "verify")
        if vrow is None:
            return _html_page(
                "验证失败",
                "<h1>链接无效或已过期</h1><p>请重新注册,或在个人资料里重新发送验证邮件。</p>",
            )
        user = (
            await session.execute(select(UserModel).where(UserModel.id == vrow.user_id))
        ).scalar_one_or_none()
        if user is None:
            return _html_page("验证失败", "<h1>账号不存在</h1>")
        user.email_verified = True
        await session.commit()
    return _html_page("验证成功", "<h1>✓ 邮箱已验证</h1><p>现在可以返回客户端登录了。</p>")


@app.post("/auth/resend-verification")
async def resend_verification(body: ResendVerificationRequest, request: Request) -> dict:
    """Re-send the verification email (60 s Redis cooldown per address)."""
    email = body.email.strip().lower()
    key = f"verify:{email}"
    redis = getattr(request.app.state, "redis", None)
    if redis is not None and await redis.get(key):
        raise HTTPException(status_code=429, detail="发送过于频繁,请稍后再试(60 秒)。")
    async with SessionLocal() as session:
        user = (
            await session.execute(select(UserModel).where(UserModel.email == email))
        ).scalar_one_or_none()
        if user is None or user.email_verified:
            return {"status": "ok", "message": "如果该邮箱已注册且未验证,验证邮件将重新发送。"}
        raw = await _issue_verification(session, user.id, "verify", 1440)
    if redis is not None:
        await redis.setex(key, 60, "1")
    base = str(request.base_url).rstrip("/")
    dev = await _send_account_email("DeepDive 邮箱验证", email, base, raw, "auth/verify-email")
    return {"status": "ok", "message": "验证邮件已重新发送,请查收。", **dev}


@app.post("/auth/forgot-password")
async def forgot_password(body: ForgotPasswordRequest, request: Request) -> dict:
    """Email a one-time password-reset link (does not reveal whether the email exists)."""
    email = body.email.strip().lower()
    async with SessionLocal() as session:
        user = (
            await session.execute(select(UserModel).where(UserModel.email == email))
        ).scalar_one_or_none()
        if user is None:
            return {"status": "ok", "message": "如果该邮箱已注册,重置邮件将发送到您的邮箱。"}
        raw = await _issue_verification(session, user.id, "reset", 60)
    base = str(request.base_url).rstrip("/")
    dev = await _send_account_email("DeepDive 密码重置", email, base, raw, "auth/reset-password")
    return {"status": "ok", "message": "重置邮件已发送,请查收(1 小时内有效)。", **dev}


@app.get("/auth/reset-password")
async def reset_password_page(token: str) -> HTMLResponse:
    """Browser landing for the reset link: a small form that POSTs JSON to /auth/reset-password."""
    body = (
        "<h1>重置密码</h1><p>请输入新密码(至少 6 位)。</p>"
        '<form id="reset-form"><input type="hidden" id="reset-token" value="'
        + token
        + '">'
        '<input type="password" id="reset-password" placeholder="新密码" minlength="6" autocomplete="new-password" required>'
        '<input type="password" id="reset-password2" placeholder="确认新密码" required>'
        '<button type="submit">重置密码</button>'
        '<p class="err" id="reset-err"></p></form>'
        "<script>"
        "document.getElementById('reset-form').addEventListener('submit',async e=>{e.preventDefault();"
        "const p=document.getElementById('reset-password').value,p2=document.getElementById('reset-password2').value,"
        "err=document.getElementById('reset-err');"
        "if(p!==p2){err.textContent='两次输入的密码不一致';return;}"
        "const r=await fetch('/auth/reset-password',{method:'POST',"
        "headers:{'Content-Type':'application/json'},"
        "body:JSON.stringify({token:document.getElementById('reset-token').value,password:p})});"
        "const d=await r.json().catch(()=>({}));"
        "if(r.ok){err.className='ok';err.textContent=d.message||'密码已重置,请用新密码登录。';}"
        "else{err.className='err';err.textContent=d.detail||'重置失败,请重试。';}});"
        "</script>"
    )
    return _html_page("重置密码", body)


@app.post("/auth/reset-password")
async def reset_password(body: ResetPasswordRequest) -> dict:
    """Apply a new password from a valid reset token and revoke the user's login tokens."""
    if len(body.password) < 6:
        raise HTTPException(status_code=400, detail="密码至少 6 位")
    async with SessionLocal() as session:
        vrow = await _consume_verification(session, body.token, "reset")
        if vrow is None:
            raise HTTPException(status_code=400, detail="链接无效或已过期,请重新发起重置。")
        user = (
            await session.execute(select(UserModel).where(UserModel.id == vrow.user_id))
        ).scalar_one_or_none()
        if user is None:
            raise HTTPException(status_code=400, detail="账号不存在")
        user.password_hash = hash_password(body.password)
        # Old password stops working immediately: revoke every login token for this user.
        await session.execute(
            update(LoginTokenModel)
            .where(LoginTokenModel.user_id == user.id)
            .values(is_active=False)
        )
        await session.commit()
    return {"status": "ok", "message": "密码已重置,请用新密码登录。"}


@app.patch("/auth/me")
async def update_me(
    request: Request,
    body: ProfileUpdateRequest,
    user: AuthUser = Depends(require_user),
) -> dict:
    """Self-service profile edit: display name / username / email / phone / password."""
    if body.new_password and not body.current_password:
        raise HTTPException(status_code=400, detail="修改密码需要输入当前密码")
    email_changed = False
    new_email = None
    user_id = user.user_id
    async with SessionLocal() as session:
        row = (
            await session.execute(select(UserModel).where(UserModel.id == user.user_id))
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="User not found")
        if body.username is not None:
            username = body.username.strip()
            if not username:
                raise HTTPException(status_code=400, detail="用户名不能为空")
            dup = (
                await session.execute(
                    select(UserModel).where(
                        UserModel.username == username, UserModel.id != row.id
                    )
                )
            ).scalar_one_or_none()
            if dup is not None:
                raise HTTPException(status_code=409, detail="用户名已被使用")
            row.username = username
        if body.display_name is not None:
            row.display_name = body.display_name.strip() or None
        if body.phone is not None:
            row.phone = body.phone.strip() or None
        if body.email is not None:
            email = body.email.strip().lower()
            if not email:
                raise HTTPException(status_code=400, detail="邮箱不能为空")
            if email != row.email:
                dup = (
                    await session.execute(
                        select(UserModel).where(
                            UserModel.email == email, UserModel.id != row.id
                        )
                    )
                ).scalar_one_or_none()
                if dup is not None:
                    raise HTTPException(status_code=409, detail="该邮箱已被其他账号使用")
                row.email = email
                row.email_verified = False
                email_changed = True
                new_email = email
        if body.new_password:
            if not verify_password(body.current_password or "", row.password_hash):
                raise HTTPException(status_code=400, detail="当前密码不正确")
            if len(body.new_password) < 6:
                raise HTTPException(status_code=400, detail="新密码至少 6 位")
            row.password_hash = hash_password(body.new_password)
        await session.commit()
    if email_changed:
        async with SessionLocal() as session:
            raw = await _issue_verification(session, user_id, "verify", 1440)
        base = str(request.base_url).rstrip("/")
        dev = await _send_account_email("DeepDive 邮箱验证", new_email, base, raw, "auth/verify-email")
        return {"status": "ok", "message": "资料已更新,新邮箱需验证后才能再次登录。", **dev}
    return {"status": "ok", "message": "资料已更新。"}


@app.post("/auth/me/avatar")
async def upload_avatar(
    file: UploadFile = File(...), user: AuthUser = Depends(require_user)
) -> dict:
    """Accept an avatar image (PNG/JPG/WEBP/GIF ≤ 2 MB), store it, and record its URL."""
    allowed = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}
    ext = allowed.get((file.content_type or "").lower())
    if ext is None:
        raise HTTPException(status_code=400, detail="仅支持 PNG / JPG / WEBP / GIF 图片")
    data = await file.read()
    if len(data) > 2 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="头像图片不能超过 2MB")
    if not data:
        raise HTTPException(status_code=400, detail="文件为空")
    fname = f"{user.user_id}.{ext}"
    (AVATAR_DIR / fname).write_bytes(data)
    avatar_url = f"/avatars/{fname}"
    async with SessionLocal() as session:
        row = (
            await session.execute(select(UserModel).where(UserModel.id == user.user_id))
        ).scalar_one_or_none()
        if row is not None:
            row.avatar = avatar_url
            await session.commit()
    return {"avatar": avatar_url}


# ── User management (admin-only) ──
def _masked_user(u: UserModel) -> dict:
    return {
        "id": str(u.id),
        "username": u.username,
        "display_name": u.display_name,
        "role_id": u.role_id,
        "email": u.email,
        "phone": u.phone,
        "avatar": u.avatar,
        "email_verified": u.email_verified,
        "is_active": u.is_active,
        "created_at": u.created_at.isoformat() if u.created_at else None,
    }


@app.get("/admin/users")
async def list_users(_: AuthAdmin = Depends(require_admin)) -> dict:
    """List all users (masked, no password hash)."""
    async with SessionLocal() as session:
        rows = (await session.execute(select(UserModel).order_by(UserModel.created_at))).scalars().all()
    return {"users": [_masked_user(u) for u in rows]}


@app.post("/admin/users")
async def create_user(body: UserCreateRequest, _: AuthAdmin = Depends(require_admin)) -> dict:
    """Create a user account (admin sets the initial password + role)."""
    async with SessionLocal() as session:
        existing = (
            await session.execute(select(UserModel).where(UserModel.username == body.username))
        ).scalar_one_or_none()
        if existing is not None:
            raise HTTPException(status_code=409, detail="Username already exists")
        if await get_role(session, body.role_id) is None:
            raise HTTPException(status_code=400, detail=f"Unknown role: {body.role_id}")
        row = UserModel(
            username=body.username,
            password_hash=hash_password(body.password),
            display_name=body.display_name,
            role_id=body.role_id,
            is_active=True,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return {"user": _masked_user(row)}


@app.patch("/admin/users/{user_id}")
async def update_user(
    user_id: UUID, body: UserUpdateRequest, _: AuthAdmin = Depends(require_admin)
) -> dict:
    """Update a user's profile / role / active flag / password."""
    async with SessionLocal() as session:
        row = (
            await session.execute(select(UserModel).where(UserModel.id == user_id))
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="User not found")
        if body.display_name is not None:
            row.display_name = body.display_name
        if body.role_id is not None:
            if await get_role(session, body.role_id) is None:
                raise HTTPException(status_code=400, detail=f"Unknown role: {body.role_id}")
            row.role_id = body.role_id
        if body.is_active is not None:
            row.is_active = body.is_active
        if body.password:
            row.password_hash = hash_password(body.password)
        if body.email is not None:
            row.email = body.email.strip().lower() or None
        if body.phone is not None:
            row.phone = body.phone.strip() or None
        if body.email_verified is not None:
            row.email_verified = body.email_verified
        await session.commit()
        await session.refresh(row)
        return {"user": _masked_user(row)}


@app.delete("/admin/users/{user_id}")
async def delete_user(user_id: UUID, _: AuthAdmin = Depends(require_admin)) -> dict:
    """Delete a user account (cascades to their sessions/messages/tokens)."""
    async with SessionLocal() as session:
        row = (
            await session.execute(select(UserModel).where(UserModel.id == user_id))
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="User not found")
        await session.delete(row)
        await session.commit()
    return {"status": "ok"}


# ── Role management (admin-only) ──
@app.get("/admin/roles")
async def list_roles_endpoint(_: AuthAdmin = Depends(require_admin)) -> dict:
    """Return all quota/feature roles (regular/pro/vip/admin)."""
    async with SessionLocal() as session:
        roles = await list_roles(session)
    return {"roles": [role_to_dict(r) for r in roles]}


@app.patch("/admin/roles/{role_id}")
async def update_role(
    role_id: str, body: RoleUpdateRequest, _: AuthAdmin = Depends(require_admin)
) -> dict:
    """Update a role's limits / model / features in place (takes effect immediately)."""
    async with SessionLocal() as session:
        role = await get_role(session, role_id)
        if role is None:
            raise HTTPException(status_code=404, detail="Role not found")
        for field in (
            "role_name",
            "daily_request_limit",
            "monthly_request_limit",
            "daily_token_limit",
            "rpm_limit",
            "monthly_cost_limit",
            "default_model",
            "models",
            "features",
            "is_active",
        ):
            value = getattr(body, field)
            if value is not None:
                setattr(role, field, value)
        await session.commit()
        await session.refresh(role)
        return {"role": role_to_dict(role)}


@app.post("/admin/roles")
async def create_role(body: RoleCreateRequest, _: AuthAdmin = Depends(require_admin)) -> dict:
    """Create a new quota/feature role (admin console)."""
    async with SessionLocal() as session:
        if await get_role(session, body.role_id) is not None:
            raise HTTPException(status_code=409, detail=f"Role '{body.role_id}' already exists")
        row = UserRoleModel(
            role_id=body.role_id,
            role_name=body.role_name or body.role_id,
            daily_request_limit=body.daily_request_limit,
            monthly_request_limit=body.monthly_request_limit,
            daily_token_limit=body.daily_token_limit,
            rpm_limit=body.rpm_limit,
            monthly_cost_limit=body.monthly_cost_limit,
            default_model=body.default_model,
            models=body.models,
            features=body.features,
            is_active=body.is_active,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return {"role": role_to_dict(row)}


@app.delete("/admin/roles/{role_id}")
async def delete_role(role_id: str, _: AuthAdmin = Depends(require_admin)) -> dict:
    """Delete a role, unless users still reference it (soft-deactivate instead)."""
    async with SessionLocal() as session:
        role = await get_role(session, role_id)
        if role is None:
            raise HTTPException(status_code=404, detail="Role not found")
        in_use = (
            await session.execute(
                select(func.count()).select_from(UserModel).where(UserModel.role_id == role_id)
            )
        ).scalar_one()
        if in_use > 0:
            raise HTTPException(
                status_code=409,
                detail=f"Role '{role_id}' is assigned to {in_use} user(s) — set is_active=false instead",
            )
        await session.delete(role)
        await session.commit()
    return {"status": "ok"}


# ── Token management (admin-only): login credentials + LLM-key grants ──
def _masked_token(t: LoginTokenModel) -> dict:
    return {
        "id": str(t.id),
        "user_id": str(t.user_id) if t.user_id else None,
        "name": t.name,
        "token_hash": t.token_hash,  # admin-only; masked in the UI, never reversible to the raw value
        "role": t.role,
        "role_id": t.role_id,
        "expires_at": t.expires_at.isoformat() if t.expires_at else None,
        "last_used_at": t.last_used_at.isoformat() if t.last_used_at else None,
        "is_active": t.is_active,
        "created_at": t.created_at.isoformat() if t.created_at else None,
    }


@app.get("/admin/tokens")
async def list_tokens(_: AuthAdmin = Depends(require_admin)) -> dict:
    """List all login credentials (masked; the raw value is never returned again).

    Each row also carries its pinned ``credential_id`` + channel name so the admin table
    can show the model column and open the ``llm_credentials`` detail modal on click.
    """
    async with SessionLocal() as session:
        rows = (
            await session.execute(select(LoginTokenModel).order_by(LoginTokenModel.created_at))
        ).scalars().all()
        cred_names = {
            c.id: c.name
            for c in (await session.execute(select(LLMCredentialModel))).scalars().all()
        }
    tokens = []
    for t in rows:
        d = _masked_token(t)
        d["credential_id"] = str(t.credential_id) if t.credential_id else None
        d["credential_name"] = cred_names.get(t.credential_id)
        tokens.append(d)
    return {"tokens": tokens}


@app.get("/admin/grants")
async def list_grants(_: AuthAdmin = Depends(require_admin)) -> dict:
    """List the per-user LLM-key grant matrix (``access_tokens``), user × key.

    ``is_active`` is the key-grant switch the admin flips to ban / restore a key for a user —
    it never affects login (login lives on ``login_tokens``).
    """
    async with SessionLocal() as session:
        rows = (
            await session.execute(select(AccessTokenModel).order_by(AccessTokenModel.created_at))
        ).scalars().all()
        users = {
            u.id: u.username
            for u in (await session.execute(select(UserModel))).scalars().all()
        }
        creds = {
            c.id: c
            for c in (await session.execute(select(LLMCredentialModel))).scalars().all()
        }
    grants = [
        {
            "id": str(g.id),
            "user_id": str(g.user_id) if g.user_id else None,
            "username": users.get(g.user_id) or "",
            "credential_id": str(g.credential_id) if g.credential_id else None,
            "credential_name": creds[g.credential_id].name if g.credential_id in creds else "",
            "api_key": creds[g.credential_id].api_key if g.credential_id in creds else "",
            "is_active": g.is_active,
            "created_at": g.created_at.isoformat() if g.created_at else None,
        }
        for g in rows
    ]
    return {"grants": grants}


@app.patch("/admin/grants/{grant_id}")
async def update_grant(
    grant_id: UUID, body: GrantUpdateRequest, _: AuthAdmin = Depends(require_admin)
) -> dict:
    """Grant / revoke a user's LLM key: flip the key-grant switch (never blocks login)."""
    async with SessionLocal() as session:
        g = (
            await session.execute(select(AccessTokenModel).where(AccessTokenModel.id == grant_id))
        ).scalar_one_or_none()
        if g is None:
            raise HTTPException(status_code=404, detail="Grant not found")
        g.is_active = body.is_active
        await session.commit()
        await session.refresh(g)
    return {"status": "ok", "is_active": g.is_active}


@app.post("/admin/tokens")
async def create_token(body: TokenCreateRequest, _: AuthAdmin = Depends(require_admin)) -> dict:
    """Mint an API token. ``role="admin"`` is unlimited; ``role="user"`` needs a user_id."""
    async with SessionLocal() as session:
        if body.role == "admin":
            body.user_id = None
            body.role_id = None
        else:
            if body.user_id is None:
                raise HTTPException(status_code=400, detail="user_id is required for user tokens")
            user = (
                await session.execute(select(UserModel).where(UserModel.id == body.user_id))
            ).scalar_one_or_none()
            if user is None:
                raise HTTPException(status_code=404, detail="User not found")
        if body.role_id is not None and await get_role(session, body.role_id) is None:
            raise HTTPException(status_code=404, detail="Role not found")
    raw = await _mint_token(
        role=body.role,
        name=body.name,
        user_id=body.user_id,
        role_id=body.role_id,
        expires_at=body.expires_at,
    )
    return {"token": raw, "name": body.name, "role": body.role}


@app.patch("/admin/tokens/{token_id}")
async def update_token(
    token_id: UUID, body: TokenUpdateRequest, _: AuthAdmin = Depends(require_admin)
) -> dict:
    """Rename / enable / disable / extend a login credential."""
    async with SessionLocal() as session:
        t = (
            await session.execute(select(LoginTokenModel).where(LoginTokenModel.id == token_id))
        ).scalar_one_or_none()
        if t is None:
            raise HTTPException(status_code=404, detail="Token not found")
        if body.name is not None:
            t.name = body.name
        if body.is_active is not None:
            t.is_active = body.is_active
        if body.expires_at is not None:
            t.expires_at = body.expires_at
        await session.commit()
        await session.refresh(t)
        return {"token": _masked_token(t)}


@app.delete("/admin/tokens/{token_id}")
async def delete_token(token_id: UUID, _: AuthAdmin = Depends(require_admin)) -> dict:
    """Revoke a login credential (deletes the row)."""
    async with SessionLocal() as session:
        t = (
            await session.execute(select(LoginTokenModel).where(LoginTokenModel.id == token_id))
        ).scalar_one_or_none()
        if t is None:
            raise HTTPException(status_code=404, detail="Token not found")
        await session.delete(t)
        await session.commit()
    return {"status": "ok"}


# ── Model catalog (admin-only; pricing is the PAYG cost source) ──
def _masked_model(m: LLMModelModel) -> dict:
    return {
        "id": str(m.id),
        "name": m.name,
        "provider_model_name": m.provider_model_name,
        "description": m.description,
        "prompt_price_per_1k": float(m.prompt_price_per_1k),
        "completion_price_per_1k": float(m.completion_price_per_1k),
        "is_active": m.is_active,
        "created_at": m.created_at.isoformat() if m.created_at else None,
    }


@app.get("/admin/models")
async def list_models(_: AuthAdmin = Depends(require_admin)) -> dict:
    """List the model catalog (name + per-1k pricing)."""
    async with SessionLocal() as session:
        rows = (
            await session.execute(select(LLMModelModel).order_by(LLMModelModel.created_at))
        ).scalars().all()
    return {"models": [_masked_model(m) for m in rows]}


@app.post("/admin/models")
async def create_model(body: ModelCreateRequest, _: AuthAdmin = Depends(require_admin)) -> dict:
    """Add a model to the catalog."""
    async with SessionLocal() as session:
        dup = (
            await session.execute(select(LLMModelModel).where(LLMModelModel.name == body.name))
        ).scalar_one_or_none()
        if dup is not None:
            raise HTTPException(status_code=409, detail="Model name already exists")
        row = LLMModelModel(
            name=body.name,
            provider_model_name=body.provider_model_name,
            description=body.description,
            prompt_price_per_1k=body.prompt_price_per_1k,
            completion_price_per_1k=body.completion_price_per_1k,
            is_active=body.is_active,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return {"model": _masked_model(row)}


@app.patch("/admin/models/{model_id}")
async def update_model(
    model_id: UUID, body: ModelUpdateRequest, _: AuthAdmin = Depends(require_admin)
) -> dict:
    """Update a catalog model's name / pricing / active flag."""
    async with SessionLocal() as session:
        m = await session.get(LLMModelModel, model_id)
        if m is None:
            raise HTTPException(status_code=404, detail="Model not found")
        for field in ("name", "provider_model_name", "description", "prompt_price_per_1k", "completion_price_per_1k", "is_active"):
            value = getattr(body, field)
            if value is not None:
                setattr(m, field, value)
        await session.commit()
        await session.refresh(m)
        return {"model": _masked_model(m)}


@app.delete("/admin/models/{model_id}")
async def delete_model(model_id: UUID, _: AuthAdmin = Depends(require_admin)) -> dict:
    """Remove a model from the catalog."""
    async with SessionLocal() as session:
        m = await session.get(LLMModelModel, model_id)
        if m is None:
            raise HTTPException(status_code=404, detail="Model not found")
        await session.delete(m)
        await session.commit()
    return {"status": "ok"}


# ── Provider credentials (admin-only; one row = one LLM channel/"token") ──
async def _credential_prices(session) -> dict[UUID, dict]:
    """Map credential_id → derived pricing from its active credential_models routes.

    Price = route override when set, else the llm_models catalog price. Multiple routes
    yield a min…max range (displayed as a range on the channel card).
    """
    rows = (
        await session.execute(
            select(CredentialModelModel, LLMModelModel)
            .join(LLMModelModel, LLMModelModel.id == CredentialModelModel.model_id)
            .where(CredentialModelModel.is_active.is_(True))
        )
    ).all()
    out: dict[UUID, dict] = {}
    for route, model in rows:
        d = out.setdefault(
            route.credential_id,
            {"models": [], "p_min": None, "p_max": None, "c_min": None, "c_max": None, "count": 0},
        )
        pp = float(route.prompt_price_per_1k) if route.prompt_price_per_1k is not None else float(
            model.prompt_price_per_1k
        )
        cp = float(route.completion_price_per_1k) if route.completion_price_per_1k is not None else float(
            model.completion_price_per_1k
        )
        d["models"].append(
            {
                "model_id": str(model.id),
                "model_name": model.name,
                "provider_model_name": model.provider_model_name,
                "prompt_price_per_1k": pp,
                "completion_price_per_1k": cp,
                "price_override": route.prompt_price_per_1k is not None or route.completion_price_per_1k is not None,
            }
        )
        d["count"] += 1
        d["p_min"] = pp if d["p_min"] is None else min(d["p_min"], pp)
        d["p_max"] = pp if d["p_max"] is None else max(d["p_max"], pp)
        d["c_min"] = cp if d["c_min"] is None else min(d["c_min"], cp)
        d["c_max"] = cp if d["c_max"] is None else max(d["c_max"], cp)
    return out


def _masked_credential(c: LLMCredentialModel, pricing: dict | None = None) -> dict:
    d = {
        "id": str(c.id),
        "name": c.name,
        "base_url": c.base_url,
        "api_key_set": bool(c.api_key),
        "is_active": c.is_active,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }
    if pricing is not None:
        d.update(
            {
                "model_count": pricing.get("count", 0),
                "models": pricing.get("models", []),
                "price_prompt_min": pricing.get("p_min"),
                "price_prompt_max": pricing.get("p_max"),
                "price_completion_min": pricing.get("c_min"),
                "price_completion_max": pricing.get("c_max"),
            }
        )
    return d


@app.get("/admin/credentials")
async def list_credentials(_: AuthAdmin = Depends(require_admin)) -> dict:
    """List provider channels (keys masked; each row carries its derived price + model list)."""
    async with SessionLocal() as session:
        rows = (
            await session.execute(select(LLMCredentialModel).order_by(LLMCredentialModel.created_at))
        ).scalars().all()
        pricing = await _credential_prices(session)
    return {"credentials": [_masked_credential(c, pricing.get(c.id)) for c in rows]}


@app.get("/admin/credentials/{credential_id}")
async def get_credential(credential_id: UUID, _: AuthAdmin = Depends(require_admin)) -> dict:
    """Channel detail by primary key (drives the token-management detail modal)."""
    async with SessionLocal() as session:
        c = await session.get(LLMCredentialModel, credential_id)
        if c is None:
            raise HTTPException(status_code=404, detail="Credential not found")
        pricing = (await _credential_prices(session)).get(c.id)
        roles = (
            await session.execute(
                select(RoleCredentialModel.role_id, UserRoleModel.role_name)
                .join(UserRoleModel, UserRoleModel.role_id == RoleCredentialModel.role_id)
                .where(RoleCredentialModel.credential_id == c.id)
            )
        ).all()
        d = _masked_credential(c, pricing)
        d["roles"] = [{"role_id": rid, "role_name": rname} for rid, rname in roles]
    return {"credential": d}


@app.post("/admin/credentials")
async def create_credential(
    body: CredentialCreateRequest, _: AuthAdmin = Depends(require_admin)
) -> dict:
    """Add a provider credential."""
    async with SessionLocal() as session:
        row = LLMCredentialModel(
            name=body.name, base_url=body.base_url, api_key=body.api_key, is_active=body.is_active
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return {"credential": _masked_credential(row)}


@app.patch("/admin/credentials/{credential_id}")
async def update_credential(
    credential_id: UUID, body: CredentialUpdateRequest, _: AuthAdmin = Depends(require_admin)
) -> dict:
    """Update a credential (a blank api_key keeps the stored value)."""
    async with SessionLocal() as session:
        c = await session.get(LLMCredentialModel, credential_id)
        if c is None:
            raise HTTPException(status_code=404, detail="Credential not found")
        if body.name is not None:
            c.name = body.name
        if body.base_url is not None:
            c.base_url = body.base_url
        if body.api_key:
            c.api_key = body.api_key
        if body.is_active is not None:
            c.is_active = body.is_active
        await session.commit()
        await session.refresh(c)
        return {"credential": _masked_credential(c)}


@app.delete("/admin/credentials/{credential_id}")
async def delete_credential(
    credential_id: UUID, _: AuthAdmin = Depends(require_admin)
) -> dict:
    """Remove a credential."""
    async with SessionLocal() as session:
        c = await session.get(LLMCredentialModel, credential_id)
        if c is None:
            raise HTTPException(status_code=404, detail="Credential not found")
        await session.delete(c)
        await session.commit()
    return {"status": "ok"}


@app.post("/admin/test-chat")
async def test_chat(body: ChatTestRequest, _: AuthAdmin = Depends(require_admin)) -> dict:
    """Simulate a PC-chat request for the chosen (user, role, channel) and call the LLM.

    Mirrors the real ``/chat`` routing ladder (pinned channel → role channels → anonymous
    degrade) so the admin sees exactly what a real chat would resolve to. It does NOT
    consume quota or debit the wallet, and the api_key is never returned.
    """
    route: dict = {}
    async with SessionLocal() as session:
        user = None
        token = None
        role_id = body.role_id or "anonymous"
        if body.user_id:
            user = (
                await session.execute(select(UserModel).where(UserModel.id == body.user_id))
            ).scalar_one_or_none()
            if user is None:
                raise HTTPException(status_code=404, detail="User not found")
            # A DB user row has no token_id column — the desktop client picks one of the
            # user's LoginToken rows at login. Mirror that by taking the most-recently-used
            # active token (the one a live PC chat would be carrying).
            token = (
                await session.execute(
                    select(LoginTokenModel)
                    .where(
                        LoginTokenModel.user_id == user.id,
                        LoginTokenModel.is_active.is_(True),
                    )
                    .order_by(LoginTokenModel.last_used_at.desc().nulls_last(), LoginTokenModel.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            role_id = body.role_id or user.role_id

        degraded = False
        if body.credential_id:
            cred = await session.get(LLMCredentialModel, body.credential_id)
            if cred is None:
                raise HTTPException(status_code=404, detail="Credential not found")
            if not cred.is_active:
                return {"ok": False, "route": None, "error": "该渠道已被停用,请先启用。"}
            base_url, api_key, model, business_name, credential_id = await _channel_route(session, cred, role_id)
        else:
            base_url, api_key, model, business_name, credential_id = await _resolve_chat_route(session, token, role_id)
            if user is not None and not base_url and not api_key:
                degraded = True
                role_id = "anonymous"
                base_url, api_key, model, business_name, credential_id = await _resolve_chat_route(session, None, role_id)
        prompt_price, completion_price = await get_model_prices(session, business_name)

    route = {
        "role_id": role_id,
        "base_url": base_url,
        "model": model,                              # real provider model id sent upstream
        "business_name": business_name,              # catalog display name used for billing
        "credential_id": str(credential_id) if credential_id else None,
        "prompt_price_per_1k": str(prompt_price),
        "completion_price_per_1k": str(completion_price),
        "degraded_to_anonymous": degraded,
    }
    if not base_url and not api_key:
        return {"ok": False, "route": route, "error": "解析不到可用渠道:该角色未绑定渠道,匿名档也无渠道。"}

    from openai import AsyncOpenAI

    client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=30.0, max_retries=0)
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": body.message}],
            temperature=0.3,
        )
        answer = (resp.choices[0].message.content or "").strip()
        usage = resp.usage
        return {
            "ok": True,
            "route": route,
            "answer": answer,
            "usage": {
                "prompt_tokens": usage.prompt_tokens if usage else 0,
                "completion_tokens": usage.completion_tokens if usage else 0,
            },
        }
    except Exception as err:  # noqa: BLE001 - surface the provider's failure verbatim
        return {"ok": False, "route": route, "error": str(err)}


# ── Role ↔ channel bindings (admin-only; the routing source for per-role LLM keys) ──
def _credential_summary(c: LLMCredentialModel, pricing: dict | None) -> dict:
    """Compact channel card for the token-management browser."""
    p = pricing or {}
    return {
        "id": str(c.id),
        "name": c.name,
        "base_url": c.base_url,
        "is_active": c.is_active,
        "model_count": p.get("count", 0),
        "price_prompt_min": p.get("p_min"),
        "price_prompt_max": p.get("p_max"),
        "price_completion_min": p.get("c_min"),
        "price_completion_max": p.get("c_max"),
    }


@app.get("/admin/roles/{role_id}/credentials")
async def list_role_credentials(role_id: str, _: AuthAdmin = Depends(require_admin)) -> dict:
    """List the channels currently bound to a role (with derived price + binding state)."""
    async with SessionLocal() as session:
        if await get_role(session, role_id) is None:
            raise HTTPException(status_code=404, detail="Role not found")
        pricing = await _credential_prices(session)
        binds = (
            await session.execute(
                select(RoleCredentialModel, LLMCredentialModel)
                .join(LLMCredentialModel, LLMCredentialModel.id == RoleCredentialModel.credential_id)
                .where(RoleCredentialModel.role_id == role_id)
            )
        ).all()
        creds = []
        for bind, cred in binds:
            d = _masked_credential(cred, pricing.get(cred.id))
            d["binding_is_active"] = bind.is_active
            creds.append(d)
    return {"credentials": creds}


@app.put("/admin/roles/{role_id}/credentials")
async def update_role_credentials(
    role_id: str, body: RoleCredentialsUpdateRequest, _: AuthAdmin = Depends(require_admin)
) -> dict:
    """Wholesale-replace a role's channel bindings (delete missing rows, insert new ones).

    Uses ``llm_credentials`` primary keys, per the channel-management requirement.
    """
    async with SessionLocal() as session:
        if await get_role(session, role_id) is None:
            raise HTTPException(status_code=404, detail="Role not found")
        ids = list(set(body.credential_ids))
        if ids:
            existing = set(
                (
                    await session.execute(
                        select(LLMCredentialModel.id).where(LLMCredentialModel.id.in_(ids))
                    )
                ).scalars().all()
            )
            missing = [str(i) for i in ids if i not in existing]
            if missing:
                raise HTTPException(status_code=400, detail=f"Unknown credential(s): {', '.join(missing)}")
        await session.execute(delete(RoleCredentialModel).where(RoleCredentialModel.role_id == role_id))
        for cid in ids:
            session.add(RoleCredentialModel(role_id=role_id, credential_id=cid))
        await session.commit()
    return {"status": "ok", "count": len(ids)}


@app.get("/admin/tokens/relations")
async def token_relations(_: AuthAdmin = Depends(require_admin)) -> dict:
    """Relationship graph for the token-management browser.

    Returns roles with their users and bound channels, plus each channel's bound roles,
    so the UI can render drop-down filters (role / credential / user) and drill into any
    channel's detail by primary key.
    """
    async with SessionLocal() as session:
        roles = (
            await session.execute(select(UserRoleModel).order_by(UserRoleModel.role_id))
        ).scalars().all()
        users = (await session.execute(select(UserModel))).scalars().all()
        creds = (
            await session.execute(select(LLMCredentialModel).order_by(LLMCredentialModel.created_at))
        ).scalars().all()
        pricing = await _credential_prices(session)
        binds = (await session.execute(select(RoleCredentialModel))).scalars().all()

        by_role: dict[str, list[UUID]] = defaultdict(list)
        by_cred: dict[UUID, list[str]] = defaultdict(list)
        for b in binds:
            by_role[b.role_id].append(b.credential_id)
            by_cred[b.credential_id].append(b.role_id)

        users_by_role: dict[str, list[dict]] = defaultdict(list)
        for u in users:
            users_by_role[u.role_id or "regular"].append(
                {
                    "id": str(u.id),
                    "username": u.username or "",
                    "display_name": u.display_name,
                }
            )

        roles_out = []
        for r in roles:
            bound = [c for c in creds if c.id in by_role.get(r.role_id, [])]
            roles_out.append(
                {
                    "role_id": r.role_id,
                    "role_name": r.role_name,
                    "is_active": r.is_active,
                    "users": users_by_role.get(r.role_id, []),
                    "credentials": [_credential_summary(c, pricing.get(c.id)) for c in bound],
                }
            )
        creds_out = []
        for c in creds:
            p = pricing.get(c.id) or {}
            creds_out.append(
                {
                    "id": str(c.id),
                    "name": c.name,
                    "base_url": c.base_url,
                    "is_active": c.is_active,
                    "model_count": p.get("count", 0),
                    "price_prompt_min": p.get("p_min"),
                    "price_prompt_max": p.get("p_max"),
                    "roles": by_cred.get(c.id, []),
                }
            )
    return {"roles": roles_out, "credentials": creds_out}


# ── Credential↔Model routing (admin-only; N:M weights + per-key price override) ──
def _masked_route(r: CredentialModelModel, credential_name: str, model_name: str) -> dict:
    return {
        "credential_id": str(r.credential_id),
        "credential_name": credential_name,
        "model_id": str(r.model_id),
        "model_name": model_name,
        "note": r.note,
        "priority": r.priority,
        "weight": r.weight,
        "prompt_price_per_1k": float(r.prompt_price_per_1k) if r.prompt_price_per_1k is not None else None,
        "completion_price_per_1k": (
            float(r.completion_price_per_1k) if r.completion_price_per_1k is not None else None
        ),
        "price_override": r.prompt_price_per_1k is not None or r.completion_price_per_1k is not None,
        "is_active": r.is_active,
    }


@app.get("/admin/routes")
async def list_routes(_: AuthAdmin = Depends(require_admin)) -> dict:
    """List every credential↔model route (names joined; prices may override the catalog)."""
    async with SessionLocal() as session:
        rows = (await session.execute(select(CredentialModelModel))).scalars().all()
        creds = {
            c.id: c.name
            for c in (await session.execute(select(LLMCredentialModel))).scalars().all()
        }
        models = {
            m.id: m.name
            for m in (await session.execute(select(LLMModelModel))).scalars().all()
        }
    return {
        "routes": [
            _masked_route(r, creds.get(r.credential_id, ""), models.get(r.model_id, ""))
            for r in rows
        ]
    }


@app.post("/admin/routes")
async def upsert_route(body: RouteUpsertRequest, _: AuthAdmin = Depends(require_admin)) -> dict:
    """Create or update one route (composite PK credential_id+model_id)."""
    async with SessionLocal() as session:
        if await session.get(LLMCredentialModel, body.credential_id) is None:
            raise HTTPException(status_code=404, detail="Credential not found")
        if await session.get(LLMModelModel, body.model_id) is None:
            raise HTTPException(status_code=404, detail="Model not found")
        stmt = (
            pg_insert(CredentialModelModel)
            .values(
                credential_id=body.credential_id,
                model_id=body.model_id,
                note=body.note,
                priority=body.priority,
                weight=body.weight,
                prompt_price_per_1k=body.prompt_price_per_1k,
                completion_price_per_1k=body.completion_price_per_1k,
                is_active=body.is_active,
            )
            .on_conflict_do_update(
                constraint="credential_models_pkey",
                set_={
                    "note": body.note,
                    "priority": body.priority,
                    "weight": body.weight,
                    "prompt_price_per_1k": body.prompt_price_per_1k,
                    "completion_price_per_1k": body.completion_price_per_1k,
                    "is_active": body.is_active,
                },
            )
            .returning(CredentialModelModel)
        )
        row = (await session.execute(stmt)).scalar_one()
        await session.commit()
    return {"route": _masked_route(row, "", "")}


@app.delete("/admin/routes/{credential_id}/{model_id}")
async def delete_route(
    credential_id: UUID, model_id: UUID, _: AuthAdmin = Depends(require_admin)
) -> dict:
    """Remove a credential↔model route."""
    async with SessionLocal() as session:
        row = await session.get(CredentialModelModel, (credential_id, model_id))
        if row is None:
            raise HTTPException(status_code=404, detail="Route not found")
        await session.delete(row)
        await session.commit()
    return {"status": "ok"}


# ── Wallets (admin-only; PAYG topup + ledger) ──
@app.get("/admin/wallets")
async def list_wallets(_: AuthAdmin = Depends(require_admin)) -> dict:
    """Return every user's wallet balance (username-resolved)."""
    async with SessionLocal() as session:
        wallets = (await session.execute(select(UserWalletModel))).scalars().all()
        users = {
            u.id: u.username
            for u in (await session.execute(select(UserModel))).scalars().all()
        }
    return {
        "wallets": [
            {
                "user_id": str(w.user_id),
                "username": users.get(w.user_id),
                "balance": float(w.balance),
                "currency": w.currency,
                "updated_at": w.updated_at.isoformat() if w.updated_at else None,
            }
            for w in wallets
        ]
    }


@app.post("/admin/wallets/topup")
async def wallet_topup(body: WalletTopupRequest, _: AuthAdmin = Depends(require_admin)) -> dict:
    """Credit a user's wallet (writes a ``topup`` ledger entry)."""
    if body.amount <= 0:
        raise HTTPException(status_code=400, detail="amount must be positive")
    async with SessionLocal() as session:
        user = await session.get(UserModel, body.user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")
        balance = await topup(session, body.user_id, body.amount, description=body.description)
        await session.commit()
    return {"status": "ok", "balance": float(balance)}


@app.get("/admin/wallets/{user_id}/transactions")
async def wallet_transactions(user_id: UUID, _: AuthAdmin = Depends(require_admin)) -> dict:
    """Return a user's wallet ledger (newest first)."""
    async with SessionLocal() as session:
        rows = await list_transactions(session, user_id)
    return {
        "transactions": [
            {
                "id": str(t.id),
                "type": t.type,
                "amount": float(t.amount),
                "balance_after": float(t.balance_after),
                "description": t.description,
                "created_at": t.created_at.isoformat() if t.created_at else None,
            }
            for t in rows
        ]
    }


async def _usage_report(
    session, user_id: UUID, start: str | None, end: str | None,
    model: str | None, limit: int, offset: int,
) -> dict:
    """Aggregate one user's daily counters, usage logs, and wallet ledger.

    Shared by the admin ``/admin/users/{id}/usage`` and the self-service ``/auth/usage``
    endpoints. Usage logs are paginated and filterable server-side (start/end date,
    fuzzy model name).
    """

    def _dt(s: str | None, *, end_of_day: bool) -> datetime | None:
        if not s:
            return None
        s = s.strip()
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        # Bare "YYYY-MM-DD" gets the day-boundary heuristic; full datetimes (which
        # the admin console sends, already converted from local to UTC) are used as-is.
        if end_of_day and len(s) == 10:
            dt = dt.replace(hour=23, minute=59, second=59, microsecond=999999)
        return dt

    start_dt = _dt(start, end_of_day=False)
    end_dt = _dt(end, end_of_day=True)
    limit = max(1, min(limit, 200))
    offset = max(0, offset)

    counters = (
        await session.execute(
            select(UserUsageCounterModel)
            .where(
                UserUsageCounterModel.user_id == user_id,
                UserUsageCounterModel.period_type == "day",
            )
            .order_by(UserUsageCounterModel.period_start.desc())
            .limit(30)
        )
    ).scalars().all()
    log_filters = [UserUsageLogModel.user_id == user_id]
    if start_dt is not None:
        log_filters.append(UserUsageLogModel.created_at >= start_dt)
    if end_dt is not None:
        log_filters.append(UserUsageLogModel.created_at <= end_dt)
    if model:
        log_filters.append(UserUsageLogModel.model_name.ilike(f"%{model}%"))
    total = (
        await session.execute(
            select(func.count()).select_from(UserUsageLogModel).where(*log_filters)
        )
    ).scalar_one()
    logs = (
        await session.execute(
            select(UserUsageLogModel)
            .where(*log_filters)
            .order_by(UserUsageLogModel.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
    ).scalars().all()
    txs = await list_transactions(session, user_id, limit=50)
    cred_names = {
        c.id: c.name
        for c in (
            await session.execute(select(LLMCredentialModel))
        ).scalars().all()
    }
    return {
        "counters": [
            {
                "period_start": c.period_start.isoformat(),
                "request_count": c.request_count,
                "token_count": c.token_count,
            }
            for c in counters
        ],
        "logs": [
            {
                "id": str(l.id),
                "created_at": l.created_at.isoformat() if l.created_at else None,
                "token_id": str(l.token_id) if l.token_id else None,
                "role_id": l.role_id,
                "credential_id": str(l.credential_id) if l.credential_id else None,
                "credential_name": cred_names.get(l.credential_id, "") if l.credential_id else "",
                "model_name": l.model_name,
                "tool": l.tool,
                "prompt_tokens": l.prompt_tokens,
                "completion_tokens": l.completion_tokens,
                "total_tokens": l.total_tokens,
                "cost_usd": float(l.cost_usd) if l.cost_usd is not None else None,
            }
            for l in logs
        ],
        "transactions": [
            {
                "id": str(t.id),
                "type": t.type,
                "amount": float(t.amount),
                "balance_after": float(t.balance_after),
                "description": t.description,
                "created_at": t.created_at.isoformat() if t.created_at else None,
            }
            for t in txs
        ],
        "total": total,
    }


@app.get("/admin/users/{user_id}/usage")
async def user_usage(
    user_id: UUID,
    start: str | None = None,   # ISO date/datetime — logs from this moment (inclusive)
    end: str | None = None,     # ISO date/datetime — logs until this moment (inclusive)
    model: str | None = None,   # fuzzy match on model_name
    limit: int = 20,            # logs per page
    offset: int = 0,
    _: AuthAdmin = Depends(require_admin),
) -> dict:
    """Aggregate a user's daily counters, usage logs, and wallet ledger (admin)."""
    async with SessionLocal() as session:
        user = await session.get(UserModel, user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")
        return await _usage_report(session, user_id, start, end, model, limit, offset)


@app.get("/admin/usage/by-channel")
async def usage_by_channel(
    start: str | None = None,
    end: str | None = None,
    _: AuthAdmin = Depends(require_admin),
) -> dict:
    """Aggregate usage cost per channel (credential) for the given date range.

    Billing is always the catalog model price; this groups the recorded rows by the serving
    channel so the admin can see how much ran through each provider key.
    """

    def _dt(s: str | None, *, end_of_day: bool) -> datetime | None:
        if not s:
            return None
        s = s.strip()
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if end_of_day and len(s) == 10:
            dt = dt.replace(hour=23, minute=59, second=59, microsecond=999999)
        return dt

    start_dt = _dt(start, end_of_day=False)
    end_dt = _dt(end, end_of_day=True)
    async with SessionLocal() as session:
        stmt = select(
            UserUsageLogModel.credential_id,
            func.count().label("request_count"),
            func.coalesce(func.sum(UserUsageLogModel.total_tokens), 0).label("total_tokens"),
            func.coalesce(func.sum(UserUsageLogModel.cost_usd), 0).label("total_cost"),
        ).group_by(UserUsageLogModel.credential_id)
        if start_dt is not None:
            stmt = stmt.where(UserUsageLogModel.created_at >= start_dt)
        if end_dt is not None:
            stmt = stmt.where(UserUsageLogModel.created_at <= end_dt)
        rows = (await session.execute(stmt)).all()
        cred_names = {
            c.id: c.name
            for c in (await session.execute(select(LLMCredentialModel))).scalars().all()
        }
    channels = [
        {
            "credential_id": str(cid) if cid else None,
            "credential_name": cred_names.get(cid, "") if cid else "",
            "request_count": int(count),
            "total_tokens": int(total_tokens),
            "total_cost": float(total_cost),
        }
        for cid, count, total_tokens, total_cost in rows
    ]
    channels.sort(key=lambda c: c["total_cost"], reverse=True)
    return {"channels": channels}


async def _load_config() -> dict:
    async with SessionLocal() as session:
        return await get_setting(session, "config") or {}


async def _save_config(data: dict) -> None:
    async with SessionLocal() as session:
        await set_setting(session, "config", data)


def _active_provider_from_cfg(cfg: dict) -> dict | None:
    providers = cfg.get("llm_providers", [])
    if not providers:
        return None
    active_id = cfg.get("llm_active_provider") or providers[0].get("id")
    return next((p for p in providers if p.get("id") == active_id), None)


def _merge_tools_into_legacy(cfg: dict) -> None:
    """Mirror the generic ``tools`` namespace onto the legacy flat ``web_search_*`` / ``smtp``
    keys so existing read paths (settings mirror, mailer, chat routing) keep working.

    Idempotent: safe to call on every load / save.
    """
    tools = cfg.get("tools") or {}
    ws = tools.get("web_search") or {}
    if ws.get("provider"):
        cfg["web_search_provider"] = ws["provider"]
    if ws.get("api_key"):
        cfg["web_search_api_key"] = ws["api_key"]
    if "engine_id" in ws:
        cfg["web_search_engine_id"] = ws["engine_id"] or ""
    if tools.get("smtp"):
        smtp = dict(tools["smtp"])
        smtp.setdefault("use_tls", True)
        smtp.setdefault("use_ssl", False)
        smtp.setdefault("enabled", True)
        cfg["smtp"] = smtp


def _tools_view(cfg: dict) -> dict:
    """Build the ``tools`` view for /config GET, backfilling from the legacy keys so a
    pre-tools config still shows its values in the Tools config page."""
    tools = dict(cfg.get("tools") or {})
    ws = dict(tools.get("web_search") or {})
    ws.setdefault("provider", cfg.get("web_search_provider", ""))
    if cfg.get("web_search_api_key"):
        ws.setdefault("api_key", cfg["web_search_api_key"])
    if cfg.get("web_search_engine_id") is not None:
        ws.setdefault("engine_id", cfg["web_search_engine_id"])
    tools["web_search"] = ws
    if cfg.get("smtp"):
        tools.setdefault("smtp", cfg["smtp"])
    return tools


def _apply_llm_settings(cfg: dict) -> None:
    """Mirror the active provider connection (base_url/api_key) + web-search onto settings.

    The model is intentionally NOT taken from here: it is resolved from the Model Catalog at
    chat time (see ``_fallback_model``), so the legacy config never carries a model id.
    """
    _merge_tools_into_legacy(cfg)
    # Keep the generic tools namespace available at runtime: any code can read
    # get_tool_config("<tool_id>").get("<param>") without hitting the DB per call.
    settings.tool_configs = _tools_view(cfg)
    active = _active_provider_from_cfg(cfg)
    base_url = cfg.get("llm_base_url") or (active or {}).get("base_url", "")
    api_key = cfg.get("llm_api_key") or (active or {}).get("api_key", "")
    if base_url:
        settings.llm_base_url = base_url
    if api_key:
        settings.llm_api_key = api_key
    if cfg.get("web_search_provider"):
        settings.web_search_provider = cfg["web_search_provider"]
    if cfg.get("web_search_api_key"):
        settings.web_search_api_key = cfg["web_search_api_key"]
    if "web_search_engine_id" in cfg:
        settings.web_search_engine_id = cfg["web_search_engine_id"] or ""
    llm.configure(settings.llm_api_key, settings.llm_base_url, settings.llm_model)


def _default_config() -> dict:
    """Starter provider card seeded on first boot.

    No model is stored here — the chat model always comes from the Model Catalog
    (``_fallback_model``), so the two sources can never drift.
    """
    return {
        "llm_providers": [{"id": "default", "name": "Default", "base_url": "", "api_key": ""}],
        "llm_active_provider": "default",
    }


async def _bootstrap_config(session) -> None:
    """Seed app_settings['config'] on first boot (legacy JSON, else a default provider), then apply."""
    if await get_setting(session, "config") is None:
        legacy: dict = {}
        if settings.config_path.exists():
            try:
                legacy = json.loads(settings.config_path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                legacy = {}
        await set_setting(session, "config", legacy or _default_config())
    cfg = await get_setting(session, "config") or {}
    _apply_llm_settings(cfg)


def _legacy_provider() -> list[dict]:
    """Synthesize a single provider card from the pre-multi-provider flat fields."""
    if not settings.llm_base_url and not settings.llm_model:
        return []
    return [
        {
            "id": "default",
            "name": "Default",
            "base_url": settings.llm_base_url,
            "api_key": settings.llm_api_key,
            "models": [settings.llm_model] if settings.llm_model else [],
            "model": settings.llm_model,
        }
    ]


async def _stored_providers() -> list[dict]:
    providers = (await _load_config()).get("llm_providers", [])
    return providers or _legacy_provider()


def _masked_provider(p: dict) -> dict:
    return {
        "id": p.get("id", ""),
        "name": p.get("name", ""),
        "base_url": p.get("base_url", ""),
        "api_key_set": bool(p.get("api_key")),
    }


def _masked_smtp(smtp: dict) -> dict:
    return {
        "host": smtp.get("host", ""),
        "port": smtp.get("port", 587),
        "user": smtp.get("user", ""),
        "password_set": bool(smtp.get("password")),
        "from_email": smtp.get("from_email", ""),
        "use_tls": smtp.get("use_tls", True),
        "use_ssl": smtp.get("use_ssl", False),
        "enabled": smtp.get("enabled", True),
    }


@app.get("/config")
async def get_config(_: AuthAdmin = Depends(require_admin)) -> dict:
    """Return the provider-card list (keys masked), active selection, and role list."""
    providers = await _stored_providers()
    cfg = await _load_config()
    active = cfg.get("llm_active_provider") or (providers[0]["id"] if providers else "")
    async with SessionLocal() as session:
        roles = await list_roles(session)
        fallback_model = await _fallback_model(session, "anonymous")
    return {
        "providers": [_masked_provider(p) for p in providers],
        "active_provider": active,
        "web_search_provider": settings.web_search_provider,
        "web_search_api_key_set": bool(settings.web_search_api_key),
        "web_search_engine_id": settings.web_search_engine_id,
        "smtp": _masked_smtp(cfg.get("smtp") or {}),
        "tools": _tools_view(cfg),
        "roles": [role_to_dict(r) for r in roles],
        "fallback_model": fallback_model,
    }


@app.post("/config")
async def update_config(body: ProvidersUpdateRequest, _: AuthAdmin = Depends(require_admin)) -> dict:
    """Persist provider cards (only when supplied) + web-search settings.

    The provider list is written only when ``body.providers`` is non-empty, so a
    web-search-only save from the Chat Test tab cannot wipe the stored cards. A blank
    ``api_key`` on a card means "keep the previously stored key" for that id.
    """
    cfg = await _load_config()
    previous = {p["id"]: p for p in cfg.get("llm_providers", [])}

    providers: list[dict] = []
    active_id = cfg.get("llm_active_provider") or ""
    if body.providers:
        for p in body.providers:
            data = p.model_dump()
            if not data.get("api_key") and previous.get(data["id"]):
                data["api_key"] = previous[data["id"]].get("api_key", "")
            data.pop("models", None)   # model id is resolved from the Catalog at chat time
            data.pop("model", None)
            providers.append(data)
        active_id = body.active_provider or (providers[0]["id"] if providers else "")
        active = next((p for p in providers if p["id"] == active_id), None)

        cfg["llm_providers"] = providers
        cfg["llm_active_provider"] = active_id
        # Mirror the active card's connection to the flat settings keys so the live client
        # picks them up without a restart. The model is deliberately not mirrored.
        if active:
            cfg["llm_base_url"] = active["base_url"]
            cfg["llm_api_key"] = active["api_key"]

    if body.web_search_provider:
        cfg["web_search_provider"] = body.web_search_provider
    if body.web_search_api_key:
        cfg["web_search_api_key"] = body.web_search_api_key
    # engine id is not a secret: a provided value (even empty) overwrites the stored one
    if body.web_search_engine_id is not None:
        cfg["web_search_engine_id"] = body.web_search_engine_id
    if body.web_search_provider:
        settings.web_search_provider = body.web_search_provider
    if body.web_search_api_key:
        settings.web_search_api_key = body.web_search_api_key
    if body.web_search_engine_id is not None:
        settings.web_search_engine_id = body.web_search_engine_id

    if body.smtp is not None:
        cur = cfg.get("smtp") or {}
        s = body.smtp.model_dump()
        if not s.get("password"):   # empty password = keep the stored one
            s["password"] = cur.get("password", "")
        cfg["smtp"] = s

    # Generic tools namespace: tools.<tool_id>.<param>. A blank secret keeps the stored value;
    # results are mirrored onto the legacy web_search_* / smtp keys below.
    if body.tools:
        stored = dict(cfg.get("tools") or {})
        for tool_id, params in body.tools.items():
            if not isinstance(params, dict):
                continue
            # The UI submits the tool's full intended state, so the stored dict is
            # REPLACED per tool (keys absent from the submission are dropped = deletion),
            # except a blank secret, which keeps the previously stored value. On first
            # migration the legacy flat keys seed the previous state so nothing is lost.
            prev = stored.get(tool_id)
            if not prev:
                if tool_id == "smtp":
                    prev = cfg.get("smtp") or {}
                elif tool_id == "web_search":
                    prev = {"provider": cfg.get("web_search_provider", "")}
                    if cfg.get("web_search_api_key"):
                        prev["api_key"] = cfg["web_search_api_key"]
                    if cfg.get("web_search_engine_id") is not None:
                        prev["engine_id"] = cfg["web_search_engine_id"]
            prev = dict(prev or {})
            merged = {}
            for k, v in params.items():
                if k in ("password", "api_key", "secret") and v == "":
                    if k in prev:
                        merged[k] = prev[k]
                    continue
                merged[k] = v
            stored[tool_id] = merged
        cfg["tools"] = stored
        _merge_tools_into_legacy(cfg)

    await _save_config(cfg)
    _apply_llm_settings(cfg)

    return {
        "status": "ok",
        "providers": [_masked_provider(p) for p in providers],
        "active_provider": active_id,
        "smtp": _masked_smtp(cfg.get("smtp") or {}),
    }


@app.post("/config/test-email")
async def test_email(body: TestEmailRequest, _: AuthAdmin = Depends(require_admin)) -> dict:
    """Send a probe email through the configured SMTP (for the admin Settings card)."""
    smtp = await _smtp_config()
    try:
        await send_email(
            smtp,
            body.to_email.strip(),
            "DeepDive 测试邮件",
            "<p>这是一封来自 DeepDive 的测试邮件,SMTP 配置正常。</p>",
        )
        return {"status": "ok", "message": "测试邮件已发送。"}
    except MailNotConfigured:
        raise HTTPException(status_code=400, detail="SMTP 未配置:请先在 Settings 里填写 SMTP 信息。")
    except Exception as err:  # noqa: BLE001 — surface the smtplib error to the admin
        raise HTTPException(status_code=400, detail=f"发送失败:{err}")


@app.post("/config/probe-models")
async def probe_models(body: ProbeModelsRequest, _: AuthAdmin = Depends(require_admin)) -> dict:
    """List model ids from an OpenAI-compatible endpoint (for the settings UI's connectivity test).

    A blank ``api_key`` falls back to the stored key of the provider whose ``base_url``
    matches, so the Live Chat card can test a configured (masked) key without retyping it.
    """
    from openai import AsyncOpenAI

    api_key = body.api_key
    if not api_key:
        cfg = await _load_config()
        want = (body.base_url or "").rstrip("/")
        for p in cfg.get("llm_providers", []):
            if p.get("base_url") and p["base_url"].rstrip("/") == want:
                api_key = p.get("api_key", "")
                break

    client = AsyncOpenAI(
        base_url=body.base_url or None,
        api_key=api_key or "sk-placeholder",
        timeout=15.0,    # fail a connectivity probe fast instead of the SDK's 10-minute read timeout
        max_retries=0,   # the SDK retries timeouts 2x by default (3 x 15s = 45s); one attempt is enough
    )
    try:
        models = await client.models.list()
    except Exception as err:  # noqa: BLE001 - surface the provider's failure verbatim
        raise HTTPException(status_code=400, detail=str(err))
    ids = [m.id for m in models.data]
    return {"models": ids}


# ── Vocabulary domain ──
@app.post("/domains")
async def create_domain(body: DomainCreate, svc=Depends(get_vocab_service)):
    return await svc.add_domain(body.name)


@app.get("/domains")
async def list_domains(svc=Depends(get_vocab_service)):
    return await svc.list_domains()


@app.post("/terms")
async def create_term(body: TermCreate, svc=Depends(get_vocab_service)):
    return await svc.add_term(body.domain_id, body.word, body.definition)


@app.get("/domains/{domain_id}/terms")
async def list_terms(domain_id: UUID, svc=Depends(get_vocab_service)):
    return await svc.list_terms(domain_id)


@app.post("/terms/update")
async def update_term(body: TermUpdate, svc=Depends(get_vocab_service)):
    await svc.update_term(
        body.term_id,
        body.definition,
        body.audio_hash,
        body.star_level,
        body.image_paths,
        body.is_active,
    )
    return {"status": "ok"}


@app.post("/terms/bulk-update")
async def bulk_update_terms(body: BulkUpdateRequest, svc=Depends(get_vocab_service)):
    updates = [
        {
            "id": u.term_id,
            "word": u.word,
            "definition": u.definition,
            "star_level": u.star_level,
            "is_active": u.is_active,
            "frequency": u.frequency,
        }
        for u in body.updates
    ]
    await svc.bulk_update_terms(updates)
    return {"status": "ok"}


@app.post("/terms/import")
async def import_terms(body: ImportRequest, svc=Depends(get_vocab_service)):
    return await svc.import_terms(body.domain_id, body.text)


@app.post("/terms/import-structured")
async def import_terms_structured(body: TermImportRequest, svc=Depends(get_vocab_service)):
    items = [(i.word, i.definition, i.frequency, i.star_level) for i in body.items]
    return await svc.import_terms_structured(body.domain_id, items)


@app.post("/sentences/import")
async def import_sentences(body: ImportRequest, svc=Depends(get_vocab_service)):
    return await svc.import_sentences(body.domain_id, body.text)


@app.post("/sentences/import-structured")
async def import_sentences_structured(body: SentenceImportRequest, svc=Depends(get_vocab_service)):
    return await svc.import_sentences_structured(body.domain_id, body.items)


@app.post("/image-fetch")
async def fetch_images(body: ImageFetchRequest, queue: TaskQueue = Depends(get_task_queue)):
    job_id = await queue.enqueue(
        IMAGE_FETCH,
        {
            "word": body.word,
            "definition": body.definition,
            "context": body.context,
            "regenerate": body.regenerate,
        },
    )
    return {"job_id": str(job_id)}


@app.post("/media/generate")
async def generate_media(body: MediaGenerateRequest, queue: TaskQueue = Depends(get_task_queue)):
    """Enqueue PPT/PDF generation from a local video (subtitles + keyframes)."""
    job_id = await queue.enqueue(
        GENERATE_MEDIA,
        {
            "video_path": body.video_path,
            "subtitle_path": body.subtitle_path,
            "format": body.format,
            "title": body.title,
        },
    )
    return {"job_id": str(job_id)}


# ── Sentences ──
@app.post("/sentences")
async def create_sentence(body: SentenceCreate, svc=Depends(get_vocab_service)):
    return await svc.add_sentence(body.domain_id, body.content_en)


@app.post("/sentences/update")
async def update_sentence(body: SentenceUpdate, svc=Depends(get_vocab_service)):
    await svc.update_sentence(body.sentence_id, body.content_cn, body.audio_hash)
    return {"status": "ok"}


@app.get("/domains/{domain_id}/sentences")
async def list_sentences(domain_id: UUID, svc=Depends(get_vocab_service)):
    return await svc.list_sentences(domain_id)


@app.get("/domains/{domain_id}/sentences/search")
async def search_sentences(domain_id: UUID, q: str, svc=Depends(get_vocab_service)):
    return await svc.search_sentences(domain_id, q)


@app.post("/domains/{domain_id}/sentences/index")
async def index_sentences(domain_id: UUID, queue: TaskQueue = Depends(get_task_queue)):
    job_id = await queue.enqueue(INDEX_SENTENCES, {"domain_id": str(domain_id)})
    return {"job_id": str(job_id)}


@app.get("/domains/{domain_id}/sentences/semantic")
async def semantic_search(domain_id: UUID, q: str, svc=Depends(get_vocab_service)):
    return await svc.search_sentences_semantic(domain_id, q)


# ── Term ↔ sentence relations ──
@app.post("/matches")
async def link_term_to_sentence(body: MatchCreate, svc=Depends(get_vocab_service)):
    await svc.link_term_to_sentence(body.term_id, body.sentence_id, body.explanation)
    return {"status": "ok"}


@app.get("/terms/{term_id}/sentences")
async def list_sentences_for_term(term_id: UUID, svc=Depends(get_vocab_service)):
    return await svc.list_sentences_for_term(term_id)


# ── TTS ──
@app.post("/tts")
async def synthesize_audio(body: TTSRequest, queue: TaskQueue = Depends(get_task_queue)):
    job_id = await queue.enqueue(TTS, {"text": body.text})
    return {"job_id": str(job_id)}


# ── AI capabilities ──
@app.post("/explain")
async def explain(body: ExplainRequest, queue: TaskQueue = Depends(get_task_queue)):
    job_id = await queue.enqueue(EXPLAIN, {"term": body.term, "context": body.context})
    return {"job_id": str(job_id)}


@app.post("/terms/definition")
async def generate_definition(body: GenerateDefinitionRequest, queue: TaskQueue = Depends(get_task_queue)):
    job_id = await queue.enqueue(GENERATE_DEFINITION, {"term": body.term})
    return {"job_id": str(job_id)}


@app.post("/sentences/analyze")
async def analyze_syntax(body: SyntaxAnalysisRequest, queue: TaskQueue = Depends(get_task_queue)):
    job_id = await queue.enqueue(ANALYZE_SYNTAX, {"sentence": body.sentence})
    return {"job_id": str(job_id)}


# ── Chat (Agent) ──
async def _guest_quota(redis, guest_id: UUID, detail: str | None = None) -> None:
    """Cap anonymous guest chat to ``guest_daily_limit`` requests per day (Redis counter).

    ``detail`` overrides the 429 message so a signed-in user who degraded to the anonymous
    tier gets a top-up/upgrade prompt instead of the "sign in" one aimed at true guests.
    """
    if settings.guest_daily_limit <= 0:
        return
    key = f"ratelimit:guest:{guest_id}:{datetime.now(timezone.utc).date().isoformat()}"
    count = await redis.incr(key)
    if count == 1:
        now = datetime.now(timezone.utc)
        midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        await redis.expire(key, int((midnight - now).total_seconds()) + 1)
    if count > settings.guest_daily_limit:
        raise HTTPException(
            status_code=429,
            detail=detail or "Guest limit reached — sign in to keep chatting.",
        )


async def _log_usage(
    user: AuthUser, model: str, tool: str, usage: dict | None = None,
    credential_id: UUID | None = None,
) -> None:
    """Record one usage-log row, pricing it against the catalog and debiting the wallet.

    Billing is best-effort: if the user has no funds, the deduction is skipped (the request
    still succeeds) and the usage row is written with the real cost. This keeps PAYG from
    hard-blocking a request while the admin reviews balances.

    The cost is always the catalog model price; ``credential_id`` only records which channel
    served the request so the admin can aggregate cost per channel.
    """
    usage = usage or {}
    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    completion_tokens = int(usage.get("completion_tokens") or 0)
    total_tokens = int(usage.get("total_tokens") or 0) or prompt_tokens + completion_tokens
    async with SessionLocal() as session:
        prompt_price, completion_price = await get_model_prices(session, model)
        cost = compute_cost(prompt_tokens, completion_tokens, prompt_price, completion_price)
        if cost > 0:
            await deduct(session, user.user_id, cost, description=f"chat ({model})", meta={"tool": tool})
        session.add(
            UserUsageLogModel(
                user_id=user.user_id,
                token_id=user.token_id,
                role_id=user.role.role_id,
                credential_id=credential_id,
                model_name=model,
                tool=tool,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                cost_usd=float(cost),
            )
        )
        await session.commit()


@app.post("/chat")
async def chat(
    body: ChatRequest,
    request: Request,
    user: AuthUser | None = Depends(require_user_optional),
    queue: TaskQueue = Depends(get_task_queue),
):
    # Resolve the LLM channel for this request: a logged-in user uses the channel pinned on
    # their token at login (failing over to another active channel of the same role); a guest
    # uses the ``anonymous`` role's channels. A role with no usable channel falls back to the
    # legacy /config route (empty base_url/api_key → the configured global client).
    #
    # A logged-in user whose every LLM key is disabled on the Tokens page has *no* usable
    # channel — they still log in fine, but degrade to the anonymous tier for this request:
    # guest daily quota + anonymous routing (that's the "equivalent to an anonymous user"
    # behavior; full access returns when the admin re-enables a key).
    notice = None
    async with SessionLocal() as session:
        if user is None:
            user_id = body.user_id or await ensure_user(SessionLocal)
            await _guest_quota(request.app.state.redis, user_id)
            token = None
            role_id = "anonymous"
            log_user = None
        else:
            user_id = user.user_id
            token = await session.get(LoginTokenModel, user.token_id)
            role_id = user.role.role_id
            log_user = user
        base_url, api_key, model, business_name, credential_id = await _resolve_chat_route(session, token, role_id)
        if user is not None and not base_url and not api_key:
            anon = await get_role(session, "anonymous")
            anon_limit = anon.daily_request_limit if anon is not None else settings.guest_daily_limit
            limit_txt = f"每天限 {anon_limit} 次" if anon_limit >= 0 else "按匿名用户限额"
            await _guest_quota(
                request.app.state.redis, user_id,
                detail="你的额度已用完,且匿名额度也已用完。请充值或升级套餐后继续使用。",
            )
            role_id = "anonymous"
            log_user = None
            notice = (
                f"你的渠道额度已用完,已按匿名用户身份继续使用({limit_txt})。"
                "如需更多额度,请充值或升级套餐。"
            )
            base_url, api_key, model, business_name, credential_id = await _resolve_chat_route(session, None, role_id)
        elif user is not None:
            await check_quota(session, user.user_id, user.role)
        # The anonymous tier has no channel either: do NOT fall back to the legacy global
        # connection — tell the user instead (the admin must bind a channel to the role).
        if not base_url and not api_key:
            raise HTTPException(
                status_code=503,
                detail="当前没有可用的 LLM 渠道,无法使用聊天。请联系管理员配置渠道,或充值/升级套餐后重试。",
            )

    session_id = body.session_id or await create_session(SessionLocal, user_id)
    session_memory = SessionMemoryStore(SessionLocal, _embedder(), llm, session_id, user_id)
    history = body.history or await session_memory.load_messages()
    result = await get_agent().run(
        body.message,
        history,
        session_memory=session_memory,
        model=model,
        base_url=base_url or None,
        api_key=api_key or None,
    )
    # close() (inside run) already flushed events; defer the expensive embed+summary work.
    await queue.enqueue(SESSION_FINALIZE, {"session_id": str(session_id)})
    if log_user is not None:
        await _log_usage(log_user, business_name, "chat", result.usage, credential_id=credential_id)
    resp = {
        "answer": result.final_answer,
        "messages": result.messages,
        "session_id": str(session_id),
        "user_id": str(user_id),
    }
    if notice:
        resp["notice"] = notice
    return resp


@app.get("/sessions")
async def get_sessions(user: AuthUser = Depends(require_user)) -> dict:
    """List the authenticated user's chat sessions (newest first)."""
    return {"sessions": await list_sessions(SessionLocal, user.user_id)}


@app.get("/sessions/{session_id}")
async def get_session_messages(session_id: UUID, user: AuthUser = Depends(require_user)):
    """Return a session's message history (resume) if it belongs to the current user."""
    async with SessionLocal() as session:
        sess = (
            await session.execute(select(SessionModel).where(SessionModel.id == session_id))
        ).scalar_one_or_none()
    if sess is None or sess.user_id != user.user_id:
        raise HTTPException(status_code=404, detail="session not found")
    messages = await load_session_messages(SessionLocal, session_id)
    return {"session_id": str(session_id), "messages": messages}


@app.get("/jobs/{job_id}")
async def get_job(job_id: UUID, queue: TaskQueue = Depends(get_task_queue)):
    """Return the state of an async enrichment job (single source of truth: the jobs table)."""
    return await queue.get(job_id)


@app.post("/chat/stream")
async def chat_stream(
    body: ChatRequest,
    request: Request,
    user: AuthUser = Depends(require_user),
):
    """SSE streaming. A user with no usable LLM key degrades to the anonymous tier (guest quota)."""
    notice = None
    async with SessionLocal() as session:
        token = await session.get(LoginTokenModel, user.token_id)
        base_url, api_key, model, business_name, credential_id = await _resolve_chat_route(session, token, user.role.role_id)
        if not base_url and not api_key:
            anon = await get_role(session, "anonymous")
            anon_limit = anon.daily_request_limit if anon is not None else settings.guest_daily_limit
            limit_txt = f"每天限 {anon_limit} 次" if anon_limit >= 0 else "按匿名用户限额"
            await _guest_quota(
                request.app.state.redis, user.user_id,
                detail="你的额度已用完,且匿名额度也已用完。请充值或升级套餐后继续使用。",
            )
            base_url, api_key, model, business_name, credential_id = await _resolve_chat_route(session, None, "anonymous")
            notice = (
                f"你的渠道额度已用完,已按匿名用户身份继续使用({limit_txt})。"
                "如需更多额度,请充值或升级套餐。"
            )
            log_user = None
        else:
            await check_quota(session, user.user_id, user.role)
            log_user = user
    # All keys + the anonymous tier are exhausted too — block, don't fall back to the
    # legacy global connection; the user must top up / upgrade.
    if not base_url and not api_key:
        raise HTTPException(
            status_code=503,
            detail="当前没有可用的 LLM 渠道,无法使用聊天。请联系管理员配置渠道,或充值/升级套餐后重试。",
        )
    if log_user is not None:
        await _log_usage(log_user, business_name, "chat_stream", credential_id=credential_id)

    async def gen():
        if notice:
            yield {"notice": notice}
        async for chunk in llm.complete_stream(
            body.message, model=model, base_url=base_url or None, api_key=api_key or None
        ):
            yield {"data": chunk}

    return EventSourceResponse(gen())
