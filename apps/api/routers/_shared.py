"""Cross-router helpers shared by auth / admin / chat / config routes.

Each helper keeps its own module logger. The chat routing ladder (``_resolve_chat_route``
and friends) resolves which LLM channel serves a request; ``_usage_report`` / ``_log_usage`` /
``_guest_quota`` back the billing and quota paths; ``_masked_model`` is the catalog view.
"""
from __future__ import annotations

import logging
import random
from datetime import UTC, datetime, timedelta
from uuid import UUID

from api.auth import AuthUser
from core.config import settings
from core.infrastructure.billing import compute_cost, deduct, get_model_prices, list_transactions
from core.infrastructure.db import (
    AccessTokenModel,
    CredentialModelModel,
    LLMCredentialModel,
    LLMModelModel,
    LoginTokenModel,
    RoleCredentialModel,
    SessionLocal,
    UserModel,
    UserUsageCounterModel,
    UserUsageLogModel,
)
from core.infrastructure.security import get_role, verify_password
from fastapi import HTTPException
from sqlalchemy import func, select

logger = logging.getLogger(__name__)


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
    return datetime.now(UTC) + timedelta(minutes=settings.access_token_expire_minutes)


async def _verify_user_login(session, username: str, password: str) -> UserModel:
    """Validate a user's credentials + account state; raise 401/403 with client hints."""
    row = (
        await session.execute(select(UserModel).where(UserModel.username == username))
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
    if not verify_password(password, row.password_hash):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    return row


async def _guest_quota(redis, guest_id: UUID, detail: str | None = None) -> None:
    """Cap anonymous guest chat to ``guest_daily_limit`` requests per day (Redis counter).

    ``detail`` overrides the 429 message so a signed-in user who degraded to the anonymous
    tier gets a top-up/upgrade prompt instead of the "sign in" one aimed at true guests.
    """
    if settings.guest_daily_limit <= 0:
        return
    key = f"ratelimit:guest:{guest_id}:{datetime.now(UTC).date().isoformat()}"
    count = await redis.incr(key)
    if count == 1:
        now = datetime.now(UTC)
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
            dt = dt.replace(tzinfo=UTC)
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
