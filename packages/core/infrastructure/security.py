"""Auth + quota primitives: password/token hashing, roles, usage counters, settings.

No external hashing dependency (bcrypt/passlib) is available, so passwords use the stdlib
pbkdf2_hmac; opaque API tokens are stored as sha256 hashes (the raw value is returned to
the client exactly once). Usage quota is enforced with an atomic UPSERT on
``user_usage_counters`` — not Redis (volatile) and not COUNT over the log table (slow).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import date, datetime, timezone
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from core.infrastructure.db import (
    AppSettingModel,
    UserModel,
    UserRoleModel,
    UserUsageCounterModel,
)

_PBKDF2_ITERATIONS = 100_000


# ── Passwords ──
def hash_password(password: str) -> str:
    """Return a salted pbkdf2 hash string: ``pbkdf2_sha256$<iterations>$<salt>$<digest>``."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(
        _PBKDF2_ITERATIONS,
        base64.b64encode(salt).decode(),
        base64.b64encode(digest).decode(),
    )


def verify_password(password: str, stored: str | None) -> bool:
    """Constant-time check of ``password`` against a stored ``hash_password`` string."""
    if not stored:
        return False
    try:
        algo, iterations, salt_b64, digest_b64 = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, int(iterations))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


# ── Opaque API tokens ──
def hash_token(token: str) -> str:
    """Return the sha256 hex digest of a raw token (what we store)."""
    return hashlib.sha256(token.encode()).hexdigest()


def generate_token() -> tuple[str, str]:
    """Mint a new opaque token, returning ``(raw, hash)``; only ``raw`` is shown once."""
    raw = "dd_" + secrets.token_urlsafe(32)
    return raw, hash_token(raw)


# ── Settings (app_settings key/value JSONB) ──
async def get_setting(session, key: str) -> dict | None:
    """Return the JSON value stored under ``key`` in app_settings, or None."""
    row = (
        await session.execute(select(AppSettingModel).where(AppSettingModel.key == key))
    ).scalar_one_or_none()
    return row.value if row is not None else None


async def set_setting(session, key: str, value: dict) -> None:
    """Upsert ``value`` under ``key`` in app_settings."""
    row = (
        await session.execute(select(AppSettingModel).where(AppSettingModel.key == key))
    ).scalar_one_or_none()
    if row is None:
        session.add(AppSettingModel(key=key, value=value))
    else:
        row.value = value
    await session.commit()


async def ensure_default_admin(session) -> None:
    """Seed the default admin credential (admin/admin) on first boot if none exists."""
    if await get_setting(session, "admin") is not None:
        return
    await set_setting(
        session,
        "admin",
        {"username": "admin", "password_hash": hash_password("admin")},
    )


async def ensure_admin_user(session) -> None:
    """Mirror the admin credential into the users table so admin/admin also logs into /auth/login.

    The admin console authenticates against ``app_settings['admin']``; this keeps a parallel
    ``users`` row (role_id ``admin``, unlimited) so the same credentials work in the desktop
    client. The password hash is re-synced on every boot in case it changed.
    """
    admin = await get_setting(session, "admin")
    if not admin:
        return
    username = admin.get("username", "admin")
    row = (
        await session.execute(select(UserModel).where(UserModel.username == username))
    ).scalar_one_or_none()
    if row is None:
        session.add(
            UserModel(
                username=username,
                password_hash=admin.get("password_hash"),
                display_name="Admin",
                role_id="admin",
                is_active=True,
            )
        )
    else:
        row.password_hash = admin.get("password_hash")
        row.role_id = "admin"
        row.is_active = True
    await session.commit()


# ── Roles ──
async def get_role(session, role_id: str) -> UserRoleModel | None:
    """Load a role row by id."""
    return (
        await session.execute(select(UserRoleModel).where(UserRoleModel.role_id == role_id))
    ).scalar_one_or_none()


async def list_roles(session) -> list[UserRoleModel]:
    """Return all roles in creation order."""
    return (
        await session.execute(select(UserRoleModel).order_by(UserRoleModel.created_at))
    ).scalars().all()


def role_to_dict(role: UserRoleModel) -> dict:
    """Serialize a role row to a JSON-safe dict (Decimal → float)."""
    return {
        "role_id": role.role_id,
        "role_name": role.role_name,
        "daily_request_limit": role.daily_request_limit,
        "monthly_request_limit": role.monthly_request_limit,
        "daily_token_limit": role.daily_token_limit,
        "rpm_limit": role.rpm_limit,
        "monthly_cost_limit": (
            float(role.monthly_cost_limit) if role.monthly_cost_limit is not None else -1
        ),
        "default_model": role.default_model,
        "models": role.models or [],
        "features": role.features or {},
        "is_active": role.is_active,
    }


# ── Usage counters + quota ──
async def increment_usage(
    session, user_id: UUID, period_type: str, period_start: date, requests: int = 1, tokens: int = 0
) -> tuple[int, int]:
    """Atomically bump a (user, period) counter row, returning the new ``(requests, tokens)``."""
    stmt = (
        pg_insert(UserUsageCounterModel)
        .values(
            user_id=user_id,
            period_type=period_type,
            period_start=period_start,
            request_count=requests,
            token_count=tokens,
        )
        .on_conflict_do_update(
            index_elements=["user_id", "period_type", "period_start"],
            set_={
                "request_count": UserUsageCounterModel.request_count + requests,
                "token_count": UserUsageCounterModel.token_count + tokens,
                "updated_at": func.now(),
            },
        )
        .returning(
            UserUsageCounterModel.request_count, UserUsageCounterModel.token_count
        )
    )
    row = (await session.execute(stmt)).first()
    return int(row[0]), int(row[1])


async def check_quota(
    session, user_id: UUID, role: UserRoleModel, requests: int = 1, tokens: int = 0
) -> None:
    """Increment the user's day+month counters and raise 429 if any role limit is exceeded.

    The increment is intentional even when a limit is exceeded: the denied request still
    consumes quota (no free retries). ``-1`` on a limit means unlimited.
    """
    today = datetime.now(timezone.utc).date()
    month_start = today.replace(day=1)
    day_requests, day_tokens = await increment_usage(
        session, user_id, "day", today, requests, tokens
    )
    month_requests, _ = await increment_usage(
        session, user_id, "month", month_start, requests, tokens
    )
    await session.commit()

    if role.daily_request_limit >= 0 and day_requests > role.daily_request_limit:
        raise HTTPException(
            status_code=429,
            detail=f"今日请求次数已达上限({role.daily_request_limit} 次/天,角色 {role.role_name})。请充值或升级套餐后继续使用。",
        )
    if role.monthly_request_limit >= 0 and month_requests > role.monthly_request_limit:
        raise HTTPException(
            status_code=429,
            detail=f"本月请求次数已达上限({role.monthly_request_limit} 次/月,角色 {role.role_name})。请充值或升级套餐后继续使用。",
        )
    if role.daily_token_limit >= 0 and day_tokens > role.daily_token_limit:
        raise HTTPException(
            status_code=429,
            detail=f"今日 token 用量已达上限({role.daily_token_limit}/天,角色 {role.role_name})。请充值或升级套餐后继续使用。",
        )


async def get_user(session, user_id: UUID) -> UserModel | None:
    """Load a user row by id."""
    return (
        await session.execute(select(UserModel).where(UserModel.id == user_id))
    ).scalar_one_or_none()
