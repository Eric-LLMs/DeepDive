"""Admin console routes: auth, users/roles/tokens/grants/models/credentials/routes/
wallets CRUD, chat test, and usage aggregation. Serves the self-contained admin SPA.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from api.auth import ADMIN_ROLE, AuthAdmin, require_admin, sign_console_token, verify_admin
from api.routers._shared import (
    _channel_route,
    _login_expiry,
    _masked_model,
    _resolve_chat_route,
    _usage_report,
)
from api.schemas import (
    AdminLoginRequest,
    ChatTestRequest,
    CredentialCreateRequest,
    CredentialUpdateRequest,
    GrantUpdateRequest,
    ModelCreateRequest,
    ModelUpdateRequest,
    RoleCreateRequest,
    RoleCredentialsUpdateRequest,
    RoleUpdateRequest,
    RouteUpsertRequest,
    TokenCreateRequest,
    TokenUpdateRequest,
    UserCreateRequest,
    UserUpdateRequest,
    WalletTopupRequest,
)
from core.infrastructure.billing import get_model_prices, list_transactions, topup
from core.infrastructure.db import (
    AccessTokenModel,
    CredentialModelModel,
    LLMCredentialModel,
    LLMModelModel,
    LoginTokenModel,
    RoleCredentialModel,
    SessionLocal,
    UserModel,
    UserRoleModel,
    UserUsageLogModel,
    UserWalletModel,
)
from core.infrastructure.security import (
    generate_token,
    get_role,
    hash_password,
    list_roles,
    role_to_dict,
)
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

router = APIRouter(tags=["admin"])
ADMIN_DIR = Path(__file__).resolve().parent.parent / "admin"
ADMIN_INDEX = ADMIN_DIR / "index.html"


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


@router.post("/admin/login")
async def admin_login(body: AdminLoginRequest) -> dict:
    """Verify the single admin account and return a stateless console session token.

    Console sessions are signed strings held in the browser's localStorage — nothing is
    written to login_tokens, so console logins no longer accumulate duplicate rows.
    Persisted tokens (hashed in login_tokens) are only minted via the Tokens page for
    external API use.
    """
    if not await verify_admin(body.username, body.password):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = await sign_console_token(body.username, ADMIN_ROLE, _login_expiry())
    return {"access_token": token, "username": body.username}


@router.get("/admin/me")
async def admin_me(admin: AuthAdmin = Depends(require_admin)) -> dict:
    """Return the authenticated admin username (used by the admin page on load)."""
    return {"username": admin.username}


@router.get("/admin")
@router.get("/admin/")
async def admin_page() -> FileResponse:
    """Serve the self-contained admin console."""
    if not ADMIN_INDEX.exists():
        raise HTTPException(status_code=404, detail="admin console not found")
    # no-store: the console is self-contained JS under active development; a cached
    # copy masks code changes and shows stale UI (e.g. empty node params).
    return FileResponse(ADMIN_INDEX, media_type="text/html", headers={"Cache-Control": "no-store"})


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


@router.get("/admin/users")
async def list_users(_: AuthAdmin = Depends(require_admin)) -> dict:
    """List all users (masked, no password hash)."""
    async with SessionLocal() as session:
        rows = (await session.execute(select(UserModel).order_by(UserModel.created_at))).scalars().all()
    return {"users": [_masked_user(u) for u in rows]}


@router.post("/admin/users")
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


@router.patch("/admin/users/{user_id}")
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


@router.delete("/admin/users/{user_id}")
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


@router.get("/admin/roles")
async def list_roles_endpoint(_: AuthAdmin = Depends(require_admin)) -> dict:
    """Return all quota/feature roles (regular/pro/vip/admin)."""
    async with SessionLocal() as session:
        roles = await list_roles(session)
    return {"roles": [role_to_dict(r) for r in roles]}


@router.patch("/admin/roles/{role_id}")
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


@router.post("/admin/roles")
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


@router.delete("/admin/roles/{role_id}")
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


@router.get("/admin/tokens")
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


@router.get("/admin/grants")
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


@router.patch("/admin/grants/{grant_id}")
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


@router.post("/admin/tokens")
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


@router.patch("/admin/tokens/{token_id}")
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


@router.delete("/admin/tokens/{token_id}")
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


@router.get("/admin/models")
async def list_models(_: AuthAdmin = Depends(require_admin)) -> dict:
    """List the model catalog (name + per-1k pricing)."""
    async with SessionLocal() as session:
        rows = (
            await session.execute(select(LLMModelModel).order_by(LLMModelModel.created_at))
        ).scalars().all()
    return {"models": [_masked_model(m) for m in rows]}


@router.post("/admin/models")
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


@router.patch("/admin/models/{model_id}")
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


@router.delete("/admin/models/{model_id}")
async def delete_model(model_id: UUID, _: AuthAdmin = Depends(require_admin)) -> dict:
    """Remove a model from the catalog."""
    async with SessionLocal() as session:
        m = await session.get(LLMModelModel, model_id)
        if m is None:
            raise HTTPException(status_code=404, detail="Model not found")
        await session.delete(m)
        await session.commit()
    return {"status": "ok"}


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


@router.get("/admin/credentials")
async def list_credentials(_: AuthAdmin = Depends(require_admin)) -> dict:
    """List provider channels (keys masked; each row carries its derived price + model list)."""
    async with SessionLocal() as session:
        rows = (
            await session.execute(select(LLMCredentialModel).order_by(LLMCredentialModel.created_at))
        ).scalars().all()
        pricing = await _credential_prices(session)
    return {"credentials": [_masked_credential(c, pricing.get(c.id)) for c in rows]}


@router.get("/admin/credentials/{credential_id}")
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


@router.post("/admin/credentials")
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


@router.patch("/admin/credentials/{credential_id}")
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


@router.delete("/admin/credentials/{credential_id}")
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


@router.post("/admin/test-chat")
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


@router.get("/admin/roles/{role_id}/credentials")
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


@router.put("/admin/roles/{role_id}/credentials")
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


@router.get("/admin/tokens/relations")
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


@router.get("/admin/routes")
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


@router.post("/admin/routes")
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


@router.delete("/admin/routes/{credential_id}/{model_id}")
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


@router.get("/admin/wallets")
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


@router.post("/admin/wallets/topup")
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


@router.get("/admin/wallets/{user_id}/transactions")
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


@router.get("/admin/users/{user_id}/usage")
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


@router.get("/admin/usage/by-channel")
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
            dt = dt.replace(tzinfo=UTC)
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
