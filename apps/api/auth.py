"""Opaque-token authentication: server-side access_tokens + role-based quota.

Two principal kinds are minted into the same ``access_tokens`` table:

- ``admin`` tokens (``user_id`` NULL, unlimited) grant access to the ``/admin`` console
  and the protected ``/admin/*`` + ``/config`` routes.
- ``user`` tokens (``user_id`` set) grant access to ``/chat`` and ``/sessions``; the user's
  role (or an optional per-token ``role_id`` override) determines quota and model.

Tokens are opaque: only the sha256 hash is stored, and the raw value is returned once at
mint time. ``require_user`` re-reads the user row + role each request, so role changes and
deactivation take effect immediately without a re-login. Each authenticated request also
refreshes ``last_used_at``.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select, update

from core.infrastructure.db import (
    AccessTokenModel,
    SessionLocal,
    UserModel,
    UserRoleModel,
)
from core.infrastructure.security import (
    get_role,
    get_setting,
    hash_token,
    verify_password,
)

_bearer = HTTPBearer(auto_error=False)

ADMIN_ROLE = "admin"
USER_ROLE = "user"


@dataclass
class AuthUser:
    user_id: UUID
    username: str
    display_name: str | None
    role: UserRoleModel
    token_id: UUID


@dataclass
class AuthAdmin:
    username: str
    token_id: UUID


async def _lookup_token(token: str) -> AccessTokenModel:
    """Resolve a raw bearer token to its active, unexpired row; raise 401 otherwise."""
    async with SessionLocal() as session:
        row = (
            await session.execute(
                select(AccessTokenModel).where(AccessTokenModel.token_hash == hash_token(token))
            )
        ).scalar_one_or_none()
        if row is None or not row.is_active:
            raise HTTPException(status_code=401, detail="Invalid or revoked token")
        if row.expires_at is not None and row.expires_at < datetime.now(timezone.utc):
            raise HTTPException(status_code=401, detail="Token expired")
        await session.execute(
            update(AccessTokenModel)
            .where(AccessTokenModel.id == row.id)
            .values(last_used_at=datetime.now(timezone.utc))
        )
        await session.commit()
    return row


async def verify_admin(username: str, password: str) -> bool:
    """Check credentials against the admin credential stored in app_settings."""
    async with SessionLocal() as session:
        admin = await get_setting(session, "admin")
    if not admin:
        return False
    return admin.get("username") == username and verify_password(
        password, admin.get("password_hash")
    )


async def verify_user(username: str, password: str) -> dict | None:
    """Check credentials against the users table; return a masked dict on success."""
    async with SessionLocal() as session:
        row = (
            await session.execute(select(UserModel).where(UserModel.username == username))
        ).scalar_one_or_none()
    if row is None or not row.is_active:
        return None
    if not verify_password(password, row.password_hash):
        return None
    return {
        "user_id": row.id,
        "username": row.username or "",
        "display_name": row.display_name,
        "role_id": row.role_id,
    }


async def require_admin(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> AuthAdmin:
    """FastAPI dependency: return the admin identity if the token has the admin role."""
    if credentials is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = await _lookup_token(credentials.credentials)
    if token.role != ADMIN_ROLE:
        raise HTTPException(status_code=403, detail="Admin role required")
    return AuthAdmin(username=token.name, token_id=token.id)


async def require_user_optional(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> AuthUser | None:
    """FastAPI dependency: return the user if a valid token is present, else ``None``.

    ``None`` means an anonymous guest (no Authorization header). A *present but invalid*
    token still raises 401 so a stale credential forces re-login rather than silently
    dropping to the guest path.
    """
    if credentials is None:
        return None
    return await require_user(credentials)


async def require_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> AuthUser:
    """FastAPI dependency: return the authenticated user (re-checked against the DB)."""
    if credentials is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = await _lookup_token(credentials.credentials)
    if token.role != USER_ROLE or token.user_id is None:
        raise HTTPException(status_code=403, detail="User role required")
    async with SessionLocal() as session:
        user = (
            await session.execute(
                select(UserModel).where(UserModel.id == token.user_id)
            )
        ).scalar_one_or_none()
        if user is None or not user.is_active:
            raise HTTPException(status_code=401, detail="User not found or inactive")
        # Effective role: per-token override, else the user's assigned role.
        role_id = token.role_id or user.role_id
        role = await get_role(session, role_id)
    if role is None or not role.is_active:
        raise HTTPException(status_code=403, detail="Role is unavailable")
    return AuthUser(
        user_id=user.id,
        username=user.username or "",
        display_name=user.display_name,
        role=role,
        token_id=token.id,
    )
