"""Cross-router helpers shared by auth / admin / chat / config routes.

Each helper keeps its own module logger. The LLM channel ladder now lives in
:mod:`core.infrastructure.llm_routing` (the platform-wide dispatch gateway); this
module re-exports it under the historic private names so existing router imports
keep working. No routing logic lives here anymore.
``_usage_report`` / ``_log_usage`` / ``_guest_quota`` back the billing and quota
paths; ``_masked_model`` is the catalog view.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from api.auth import AuthUser, sign_guest_token, verify_guest_token
from core.config import settings
from core.infrastructure.billing import (
    compute_cost,
    deduct,
    get_balance,
    get_model_prices,
    list_transactions,
)
from core.infrastructure.db import (
    LLMCredentialModel,
    LLMModelModel,
    SessionLocal,
    UserModel,
    UserUsageCounterModel,
    UserUsageLogModel,
)

# Re-exports of the dispatch gateway under the historic router names; the per-line
# F401 suppressions exist because ruff cannot see the sibling-router imports.
from core.infrastructure.llm_routing import (
    channel_route as _channel_route,  # noqa: F401
)
from core.infrastructure.llm_routing import (
    fallback_model as _fallback_model,  # noqa: F401
)
from core.infrastructure.llm_routing import (
    pick_credential as _pick_credential,  # noqa: F401
)
from core.infrastructure.llm_routing import (
    provider_model_name as _provider_model_name,  # noqa: F401
)
from core.infrastructure.llm_routing import (
    resolve_channel_for_owner,  # noqa: F401
    resolve_effective_channel,  # noqa: F401
)
from core.infrastructure.llm_routing import (
    resolve_chat_route as _resolve_chat_route,  # noqa: F401
)
from core.infrastructure.llm_routing import (
    user_banned_from as _user_banned_from,  # noqa: F401
)
from core.infrastructure.memory import ensure_user
from core.infrastructure.security import verify_password
from fastapi import HTTPException, Request
from sqlalchemy import func, select

logger = logging.getLogger(__name__)

_AUTH_LIMITS = {
    "login": "auth_login_rpm",
    "register": "auth_register_rpm",
    "recovery": "auth_recovery_rpm",
}


async def check_rate_limit(redis, key: str, limit: int, window: int) -> bool:
    """Enforce a fixed-window rate limit; True if the call may proceed.

    Same INCR+EXPIRE-on-first pattern as ``_guest_quota``: the window starts at the first
    hit, so a quiet burst then idle resets naturally. A ``limit <= 0`` disables the limit.

    A Redis failure fails *open*: the limiter is a brute-force guard, and a Redis outage
    must never hard-block the login/register path (it only logs a warning and lets the
    request through, keeping the endpoint available over strict enforcement).
    """
    if limit <= 0:
        return True
    try:
        count = await redis.incr(key)
        if count == 1:
            await redis.expire(key, window)
        return count <= limit
    except Exception:
        logger.warning("rate-limit check failed (key=%s), failing open", key, exc_info=True)
        return True


async def _auth_rate_limit(request: Request, redis, kind: str) -> None:
    """Apply the per-kind auth rate limit, keyed by client IP (redis-less env = no-op).

    ``kind`` is one of ``_AUTH_LIMITS`` (login / register / recovery). Raises 429 when the
    fixed window is exhausted so brute-force and credential-stuffing get a hard throttle.
    """
    attr = _AUTH_LIMITS.get(kind)
    if attr is None or redis is None:
        return
    limit = getattr(settings, attr, 0)
    if limit <= 0:
        return
    client_ip = request.client.host if request.client is not None else "unknown"
    key = f"ratelimit:auth:{kind}:{client_ip}"
    if not await check_rate_limit(redis, key, limit, settings.auth_rate_limit_window):
        raise HTTPException(status_code=429, detail="请求过于频繁,请稍后再试。")


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


async def resolve_guest_identity(session_factory, guest_token: str | None) -> tuple[UUID, str | None]:
    """Resolve an anonymous request's user_id from its signed ``gt_`` token.

    Returns ``(user_id, new_token)``: when ``guest_token`` verifies, ``new_token`` is None;
    otherwise a fresh guest user is created and a token minted for it, so the client can
    persist it and stay the same guest across requests. A client-supplied user_id is never
    trusted here — the identity is always the token's embedded one (or brand-new).
    """
    if guest_token:
        uid = await verify_guest_token(guest_token)
        if uid is not None:
            return uid, None
    uid = await ensure_user(session_factory)
    expires_at = datetime.now(UTC) + timedelta(seconds=settings.guest_token_ttl_seconds)
    return uid, await sign_guest_token(uid, expires_at)


async def _guest_quota(redis, guest_id: UUID, detail: str | None = None) -> None:
    """Cap anonymous guest chat to ``guest_daily_limit`` requests per day (Redis counter).

    ``detail`` overrides the 429 message so a signed-in user who degraded to the anonymous
    tier gets a top-up/upgrade prompt instead of the "sign in" one aimed at true guests.

    Deliberately fail-*open*: the counter is a soft abuse guard, not an invariant — a Redis
    outage must not take down chat entirely, so we log a warning and let the request through.
    The cost of that leak (a guest may exceed the daily limit during an outage) is bounded
    and transient, whereas a 500 on every chat turn during an outage is not.
    """
    if settings.guest_daily_limit <= 0:
        return
    try:
        key = f"ratelimit:guest:{guest_id}:{datetime.now(UTC).date().isoformat()}"
        count = await redis.incr(key)
        if count == 1:
            now = datetime.now(UTC)
            midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            await redis.expire(key, int((midnight - now).total_seconds()) + 1)
    except Exception:
        logger.warning("guest quota counter unavailable for %s — allowing request", guest_id, exc_info=True)
        return
    if count > settings.guest_daily_limit:
        raise HTTPException(
            status_code=429,
            detail=detail or "Guest limit reached — sign in to keep chatting.",
        )


async def _log_usage(
    user: AuthUser, model: str, tool: str, usage: dict | None = None,
    credential_id: UUID | None = None, *, paid: bool = False,
) -> None:
    """Record one usage-log row, pricing it against the catalog and debiting the wallet.

    Free-quota-first model (the settlement side of ``authorize_usage``): a ``paid`` (overflow)
    request is charged its exact cost from the wallet, clamped to the available balance so a
    cost larger than the balance drains it to zero — the next overflow request then hits the
    402 gate, bounding the undercharge to one request. A ``free`` request only records the
    usage row (cost is reported for admin metrics, never charged).

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
        if paid and cost > 0:
            charge = min(cost, await get_balance(session, user.user_id))
            if charge > 0:
                await deduct(
                    session, user.user_id, charge,
                    description=f"chat ({model})", meta={"tool": tool},
                )
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
