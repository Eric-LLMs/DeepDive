"""FastAPI app: expose core use cases as REST/SSE.

Start: uvicorn api.main:app --reload
"""
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

from arq import create_pool
from arq.connections import RedisSettings
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

from api.auth import (
    AuthAdmin,
    AuthUser,
    require_admin,
    require_user,
    require_user_optional,
    verify_admin,
    verify_user,
)
from api.deps import _embedder, get_agent, get_task_queue, get_vocab_service, llm
from api.schemas import (
    AdminLoginRequest,
    BulkUpdateRequest,
    ChatRequest,
    CredentialCreateRequest,
    CredentialUpdateRequest,
    DomainCreate,
    ExplainRequest,
    GenerateDefinitionRequest,
    ImageFetchRequest,
    ImportRequest,
    LLMProviderModel,
    MatchCreate,
    MediaGenerateRequest,
    ModelCreateRequest,
    ModelUpdateRequest,
    ProvidersUpdateRequest,
    RoleUpdateRequest,
    SentenceCreate,
    SentenceImportRequest,
    SentenceUpdate,
    SyntaxAnalysisRequest,
    TermCreate,
    TermImportRequest,
    TermUpdate,
    TTSRequest,
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
    get_model_prices,
    list_transactions,
    topup,
)
from core.infrastructure.db import (
    AccessTokenModel,
    LLMCredentialModel,
    LLMModelModel,
    SessionLocal,
    SessionModel,
    UserModel,
    UserUsageLogModel,
    UserWalletModel,
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
from core.infrastructure.security import (
    check_quota,
    ensure_admin_user,
    ensure_default_admin,
    generate_token,
    get_role,
    get_setting,
    hash_password,
    list_roles,
    role_to_dict,
    set_setting,
)
from sqlalchemy import select


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
    expires_at: datetime | None = None,
) -> str:
    """Create an access_tokens row and return the raw token (shown to the client once)."""
    raw, token_hash = generate_token()
    async with SessionLocal() as session:
        session.add(
            AccessTokenModel(
                user_id=user_id,
                name=name,
                token_hash=token_hash,
                role=role,
                role_id=role_id,
                expires_at=expires_at,
            )
        )
        await session.commit()
    return raw


def _login_expiry() -> datetime:
    """Login-token lifetime, mirroring the old JWT expiry."""
    return datetime.now(timezone.utc) + timedelta(minutes=settings.access_token_expire_minutes)


# ── Admin console ──
@app.post("/admin/login")
async def admin_login(body: AdminLoginRequest) -> dict:
    """Verify the single admin account and mint an opaque admin token."""
    if not await verify_admin(body.username, body.password):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = await _mint_token(role="admin", name=body.username, expires_at=_login_expiry())
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
    """Verify a user's credentials and mint an opaque user token."""
    user = await verify_user(body.username, body.password)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = await _mint_token(
        role="user", name=user["username"], user_id=user["user_id"], expires_at=_login_expiry()
    )
    async with SessionLocal() as session:
        role = await get_role(session, user["role_id"])
    return {
        "access_token": token,
        "username": user["username"],
        "display_name": user["display_name"],
        "role_id": user["role_id"],
        "role_name": role.role_name if role else user["role_id"],
    }


@app.get("/auth/me")
async def auth_me(user: AuthUser = Depends(require_user)) -> dict:
    """Return the authenticated user's profile + role quota (desktop account panel)."""
    return {
        "user_id": str(user.user_id),
        "username": user.username,
        "display_name": user.display_name,
        "role_id": user.role.role_id,
        "role_name": user.role.role_name,
        "quota": role_to_dict(user.role),
    }


# ── User management (admin-only) ──
def _masked_user(u: UserModel) -> dict:
    return {
        "id": str(u.id),
        "username": u.username,
        "display_name": u.display_name,
        "role_id": u.role_id,
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


# ── Token management (admin-only) ──
def _masked_token(t: AccessTokenModel) -> dict:
    return {
        "id": str(t.id),
        "user_id": str(t.user_id) if t.user_id else None,
        "name": t.name,
        "role": t.role,
        "role_id": t.role_id,
        "expires_at": t.expires_at.isoformat() if t.expires_at else None,
        "last_used_at": t.last_used_at.isoformat() if t.last_used_at else None,
        "is_active": t.is_active,
        "created_at": t.created_at.isoformat() if t.created_at else None,
    }


@app.get("/admin/tokens")
async def list_tokens(_: AuthAdmin = Depends(require_admin)) -> dict:
    """List all access tokens (masked; the raw value is never returned again)."""
    async with SessionLocal() as session:
        rows = (
            await session.execute(select(AccessTokenModel).order_by(AccessTokenModel.created_at))
        ).scalars().all()
    return {"tokens": [_masked_token(t) for t in rows]}


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
    """Rename / enable / disable / extend a token."""
    async with SessionLocal() as session:
        t = (
            await session.execute(select(AccessTokenModel).where(AccessTokenModel.id == token_id))
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
    """Revoke a token (deletes the row)."""
    async with SessionLocal() as session:
        t = (
            await session.execute(select(AccessTokenModel).where(AccessTokenModel.id == token_id))
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
        for field in ("name", "description", "prompt_price_per_1k", "completion_price_per_1k", "is_active"):
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


# ── Provider credentials (admin-only; N:M model routing arrives with the retrieval service) ──
def _masked_credential(c: LLMCredentialModel) -> dict:
    return {
        "id": str(c.id),
        "name": c.name,
        "base_url": c.base_url,
        "api_key_set": bool(c.api_key),
        "is_active": c.is_active,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


@app.get("/admin/credentials")
async def list_credentials(_: AuthAdmin = Depends(require_admin)) -> dict:
    """List provider credentials (keys masked)."""
    async with SessionLocal() as session:
        rows = (
            await session.execute(select(LLMCredentialModel).order_by(LLMCredentialModel.created_at))
        ).scalars().all()
    return {"credentials": [_masked_credential(c) for c in rows]}


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


def _apply_llm_settings(cfg: dict) -> None:
    """Mirror flat LLM/web-search fields (falling back to the active provider) onto settings."""
    active = _active_provider_from_cfg(cfg)
    base_url = cfg.get("llm_base_url") or (active or {}).get("base_url", "")
    api_key = cfg.get("llm_api_key") or (active or {}).get("api_key", "")
    model = cfg.get("llm_model") or (active or {}).get("model", "")
    if base_url:
        settings.llm_base_url = base_url
    if api_key:
        settings.llm_api_key = api_key
    if model:
        settings.llm_model = model
    if cfg.get("web_search_provider"):
        settings.web_search_provider = cfg["web_search_provider"]
    if cfg.get("web_search_api_key"):
        settings.web_search_api_key = cfg["web_search_api_key"]
    llm.configure(settings.llm_api_key, settings.llm_base_url, settings.llm_model)


def _default_config() -> dict:
    """Starter provider card seeded on first boot so the admin has models to pick."""
    return {
        "llm_providers": [
            {
                "id": "default",
                "name": "Default",
                "base_url": "",
                "api_key": "",
                "models": ["gpt-4o-mini", "gpt-4.1-mini", "gpt-4o", "gpt-4.1"],
                "model": "gpt-4o-mini",
            }
        ],
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
        "models": p.get("models", []),
        "model": p.get("model", ""),
    }


@app.get("/config")
async def get_config(_: AuthAdmin = Depends(require_admin)) -> dict:
    """Return the provider-card list (keys masked), active selection, and role list."""
    providers = await _stored_providers()
    cfg = await _load_config()
    active = cfg.get("llm_active_provider") or (providers[0]["id"] if providers else "")
    async with SessionLocal() as session:
        roles = await list_roles(session)
    return {
        "providers": [_masked_provider(p) for p in providers],
        "active_provider": active,
        "web_search_provider": settings.web_search_provider,
        "web_search_api_key_set": bool(settings.web_search_api_key),
        "roles": [role_to_dict(r) for r in roles],
    }


@app.post("/config")
async def update_config(body: ProvidersUpdateRequest, _: AuthAdmin = Depends(require_admin)) -> dict:
    """Persist the full provider-card list and apply the active card to the live client.

    A blank ``api_key`` on a card means "keep the previously stored key" for that id, so
    the UI can round-trip masked keys without clearing them.
    """
    cfg = await _load_config()
    previous = {p["id"]: p for p in cfg.get("llm_providers", [])}

    providers: list[dict] = []
    for p in body.providers:
        data = p.model_dump()
        if not data.get("api_key") and previous.get(data["id"]):
            data["api_key"] = previous[data["id"]].get("api_key", "")
        providers.append(data)

    active_id = body.active_provider or (providers[0]["id"] if providers else "")
    active = next((p for p in providers if p["id"] == active_id), None)

    cfg["llm_providers"] = providers
    cfg["llm_active_provider"] = active_id
    if body.web_search_provider:
        cfg["web_search_provider"] = body.web_search_provider
    if body.web_search_api_key:
        cfg["web_search_api_key"] = body.web_search_api_key

    # Mirror the active card's fields to the flat settings keys so the live client picks
    # them up without a restart.
    if active:
        cfg["llm_base_url"] = active["base_url"]
        cfg["llm_api_key"] = active["api_key"]
        cfg["llm_model"] = active["model"]

    if body.web_search_provider:
        settings.web_search_provider = body.web_search_provider
    if body.web_search_api_key:
        settings.web_search_api_key = body.web_search_api_key

    await _save_config(cfg)
    _apply_llm_settings(cfg)

    return {
        "status": "ok",
        "providers": [_masked_provider(p) for p in providers],
        "active_provider": active_id,
    }


@app.post("/config/probe-models")
async def probe_models(body: LLMProviderModel, _: AuthAdmin = Depends(require_admin)) -> dict:
    """List model ids from an OpenAI-compatible endpoint (for the settings UI's fetch)."""
    from openai import AsyncOpenAI

    client = AsyncOpenAI(base_url=body.base_url or None, api_key=body.api_key or "sk-placeholder")
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
async def _chat_quota(user: AuthUser) -> str:
    """Enforce the user's role quota and return the model id for their role."""
    async with SessionLocal() as session:
        await check_quota(session, user.user_id, user.role)
    return user.role.default_model or settings.llm_model


async def _guest_quota(redis, guest_id: UUID) -> None:
    """Cap anonymous guest chat to ``guest_daily_limit`` requests per day (Redis counter)."""
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
            detail="Guest limit reached — sign in to keep chatting.",
        )


async def _log_usage(user: AuthUser, model: str, tool: str, usage: dict | None = None) -> None:
    """Record one usage-log row, pricing it against the catalog and debiting the wallet.

    Billing is best-effort: if the user has no funds, the deduction is skipped (the request
    still succeeds) and the usage row is written with the real cost. This keeps PAYG from
    hard-blocking a request while the admin reviews balances.
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
    if user is None:
        # Anonymous guest: reuse a persisted guest id (or mint one) and cap daily usage.
        user_id = body.user_id or await ensure_user(SessionLocal)
        await _guest_quota(request.app.state.redis, user_id)
        model = settings.llm_model
        log_user = None
    else:
        user_id = user.user_id
        model = await _chat_quota(user)
        log_user = user

    session_id = body.session_id or await create_session(SessionLocal, user_id)
    session_memory = SessionMemoryStore(SessionLocal, _embedder(), llm, session_id, user_id)
    history = body.history or await session_memory.load_messages()
    result = await get_agent().run(body.message, history, session_memory=session_memory, model=model)
    # close() (inside run) already flushed events; defer the expensive embed+summary work.
    await queue.enqueue(SESSION_FINALIZE, {"session_id": str(session_id)})
    if log_user is not None:
        await _log_usage(log_user, model, "chat", result.usage)
    return {
        "answer": result.final_answer,
        "messages": result.messages,
        "session_id": str(session_id),
        "user_id": str(user_id),
    }


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
    user: AuthUser = Depends(require_user),
):
    """SSE streaming (role quota enforced; model from the user's role)."""
    model = await _chat_quota(user)
    await _log_usage(user, model, "chat_stream")

    async def gen():
        async for token in llm.complete_stream(body.message, model=model):
            yield {"data": token}

    return EventSourceResponse(gen())
