"""Opaque-token authentication: server-side login_tokens + role-based quota.

Two principal kinds are minted into the same ``login_tokens`` table:

- ``admin`` tokens (``user_id`` NULL, unlimited) grant access to the ``/admin`` console
  and the protected ``/admin/*`` + ``/config`` routes.
- ``user`` tokens (``user_id`` set) grant access to ``/chat`` and ``/sessions``; the user's
  role (or an optional per-token ``role_id`` override) determines quota and model.

Tokens are opaque: only the sha256 hash is stored, and the raw value is returned once at
mint time. ``require_user`` re-reads the user row + role each request, so role changes and
deactivation take effect immediately without a re-login. Each authenticated request also
refreshes ``last_used_at``.
"""
import base64
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from core.infrastructure.db import (
    LoginTokenModel,
    SessionLocal,
    UserModel,
    UserRoleModel,
)
from core.infrastructure.security import (
    get_role,
    get_setting,
    hash_token,
    set_setting,
    verify_password,
)
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select, update

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
    token_id: UUID | None  # None for stateless console sessions (not stored in login_tokens)


async def _lookup_token(token: str) -> LoginTokenModel:
    """Resolve a raw bearer token to its active, unexpired login row; raise 401 otherwise."""
    async with SessionLocal() as session:
        row = (
            await session.execute(
                select(LoginTokenModel).where(LoginTokenModel.token_hash == hash_token(token))
            )
        ).scalar_one_or_none()
        if row is None or not row.is_active:
            raise HTTPException(status_code=401, detail="Invalid or revoked token")
        if row.expires_at is not None and row.expires_at < datetime.now(UTC):
            raise HTTPException(status_code=401, detail="Token expired")
        await session.execute(
            update(LoginTokenModel)
            .where(LoginTokenModel.id == row.id)
            .values(last_used_at=datetime.now(UTC))
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


# ── Stateless guest identities ──
# Anonymous guests get a signed ``gt_`` token so their user_id is server-verifiable. Without
# this, an unauthenticated client could claim any ``body.user_id`` (forging a victim's id to
# bind a session under their account / burn their guest quota). The token is signed with the
# same lazily-minted console secret; the ``guest|`` payload prefix keeps it distinct from
# ``cc_`` console sessions in the shared signing space.
GUEST_PREFIX = "gt_"


async def sign_guest_token(user_id: UUID, expires_at: datetime) -> str:
    """Mint a signed guest identity token: ``gt_<payload_b64>.<sig_hex>``.

    The payload is ``guest|user_id|expiry_ts``; the signature is HMAC-SHA256 of the payload
    with the console secret. The token is unforgeable and self-contained (no DB lookup).
    """
    secret = await _console_secret()
    payload = f"guest|{user_id}|{int(expires_at.timestamp())}"
    payload_b64 = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{GUEST_PREFIX}{payload_b64}.{sig}"


async def verify_guest_token(token: str) -> UUID | None:
    """Return the guest ``user_id`` if ``token`` is a valid, unexpired guest token."""
    if not token.startswith(GUEST_PREFIX):
        return None
    body = token[len(GUEST_PREFIX):]
    if "." not in body:
        return None
    payload_b64, sig = body.split(".", 1)
    try:
        payload = base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4)).decode()
    except (ValueError, TypeError, UnicodeDecodeError):
        return None
    secret = await _console_secret()
    expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        prefix, user_id, ts = payload.rsplit("|", 2)
        if prefix != "guest" or int(ts) < time.time():
            return None
        return UUID(user_id)
    except (ValueError, TypeError):
        return None


# ── Stateless admin console sessions ──
_CONSOLE_PREFIX = "cc_"


async def _console_secret() -> str:
    """Load (or lazily create) the HMAC secret used to sign console session tokens.

    Kept in app_settings so it survives restarts; minted once on first use. Console
    sessions are signed strings held by the browser — nothing is written to access_tokens.
    """
    async with SessionLocal() as session:
        row = await get_setting(session, "console_secret")
        if row and row.get("secret"):
            return row["secret"]
        secret = secrets.token_hex(32)
        await set_setting(session, "console_secret", {"secret": secret})
        return secret


def _sign_console_token(secret: str, username: str, role: str, expires_at: datetime) -> str:
    """Return a signed console session token: ``cc_<payload_b64>.<sig_hex>``.

    The payload is ``username|role|expiry_ts``; the signature is HMAC-SHA256 of the
    payload with the console secret, so the token is unforgeable and self-contained. The
    role claim is what keeps a user web-console session out of the admin console even
    when the user's username happens to match the admin account.
    """
    payload = f"{username}|{role}|{int(expires_at.timestamp())}"
    payload_b64 = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{_CONSOLE_PREFIX}{payload_b64}.{sig}"


def _verify_console_token(secret: str, token: str) -> tuple[str, str] | None:
    """Return ``(username, role)`` if ``token`` is a valid, unexpired console session."""
    if not token.startswith(_CONSOLE_PREFIX):
        return None
    body = token[len(_CONSOLE_PREFIX):]
    if "." not in body:
        return None
    payload_b64, sig = body.split(".", 1)
    try:
        payload = base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4)).decode()
    except (ValueError, TypeError, UnicodeDecodeError):
        return None
    expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        username, role, ts = payload.rsplit("|", 2)
        if int(ts) < time.time():
            return None
    except (ValueError, TypeError):
        return None
    return username, role


async def sign_console_token(username: str, role: str, expires_at: datetime) -> str:
    """Mint a stateless console session token (loads the signing secret from settings)."""
    secret = await _console_secret()
    return _sign_console_token(secret, username, role, expires_at)


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
    """FastAPI dependency: return the admin identity if the token has the admin role.

    Accepts both persisted API tokens (``dd_``, hashed in login_tokens) and stateless
    console session tokens (``cc_``, signed with the console secret — never stored). The
    ``cc_`` role claim must be ``admin``: user web-console sessions carry ``user`` and
    are rejected here even if their username matches the admin account.
    """
    if credentials is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    raw = credentials.credentials
    if raw.startswith(_CONSOLE_PREFIX):
        verified = _verify_console_token(await _console_secret(), raw)
        if verified is None:
            raise HTTPException(status_code=401, detail="Invalid or expired session")
        username, role = verified
        if role != ADMIN_ROLE:
            raise HTTPException(status_code=403, detail="Admin role required")
        return AuthAdmin(username=username, token_id=None)
    token = await _lookup_token(raw)
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
    """FastAPI dependency: return the authenticated user (re-checked against the DB).

    Accepts persisted API tokens (``dd_``, hashed in login_tokens) and stateless
    console-session tokens (``cc_``, signed with the console secret). The web console
    holds a ``cc_`` session so a desktop re-login (which rotates the ``dd_`` token) does
    not invalidate the browser session.
    """
    if credentials is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    raw = credentials.credentials
    if raw.startswith(_CONSOLE_PREFIX):
        verified = _verify_console_token(await _console_secret(), raw)
        if verified is None:
            raise HTTPException(status_code=401, detail="Invalid or expired session")
        username, role = verified
        if role != USER_ROLE:
            raise HTTPException(status_code=403, detail="User role required")
        async with SessionLocal() as session:
            user = (
                await session.execute(
                    select(UserModel).where(UserModel.username == username)
                )
            ).scalar_one_or_none()
            if user is None or not user.is_active:
                raise HTTPException(status_code=401, detail="User not found or inactive")
            role = await get_role(session, user.role_id)
        if role is None or not role.is_active:
            raise HTTPException(status_code=403, detail="Role is unavailable")
        return AuthUser(
            user_id=user.id,
            username=user.username or "",
            display_name=user.display_name,
            role=role,
            token_id=None,
        )
    token = await _lookup_token(raw)
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
