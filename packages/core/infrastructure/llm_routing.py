"""Unified LLM dispatch gateway — the single channel-decision funnel for the platform.

Every LLM call on the platform (chat, research, toolkit, worker jobs, retrieval) must
resolve its channel through :func:`resolve_effective_channel`. The gateway is an AND
funnel of gates, evaluated against the live DB at every call (no cached snapshots):

1. role-binding ∧ credential ``is_active``   (``role_credentials`` ∧ ``llm_credentials``)
2. user-level ban exclusion                   (disabled ``access_tokens`` grants)
3. model admission: role ``default_model`` → credential's preferred active route
   (by priority) → earliest active catalog model
4. nothing resolvable → the credential-less tuple is returned and the CALLER must
   fail-fast (API route → 503, worker job → :class:`NoActiveChannelError`).
   Falling back to a global/embedded key is forbidden.

**Credential-flow boundary:** the plaintext ``api_key`` produced here may live only in
the in-process call stack (gateway → ContextVar → AsyncOpenAI). It must never be
written to the DB, a job payload, a log line, or a trace. The resolved ``base_url``
likewise comes only from an authorized DB credential/route — payload-supplied
base_urls are never honored (SSRF defense).

:func:`resolve_chat_route` (web request, login-pinned token) and
:func:`resolve_channel_for_owner` (headless worker, owner user id) are thin adapters
that differ only in where the inputs come from; both funnel into
:func:`resolve_effective_channel` with no branching of their own.

Return contract (kept byte-compatible with the legacy ladder):
``(base_url, api_key, provider_model, business_name, credential_id)`` — empty strings
with ``credential_id is None`` mean "no channel"; callers MUST treat that as fatal.
"""
from __future__ import annotations

import logging
import random
from uuid import UUID

from sqlalchemy import select

from core.infrastructure.db import (
    AccessTokenModel,
    CredentialModelModel,
    LLMCredentialModel,
    LLMModelModel,
    LoginTokenModel,
    RoleCredentialModel,
    UserModel,
)
from core.infrastructure.security import get_role

logger = logging.getLogger(__name__)


async def pick_credential(
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


async def user_banned_from(session, user_id: UUID | None, credential_id: UUID) -> bool:
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


async def provider_model_name(session, display_name: str) -> str:
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


async def fallback_model(session, role_id: str | None = None) -> str:
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


async def channel_route(
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
        business = await fallback_model(session, role_id)
    provider = await provider_model_name(session, business)
    return credential.base_url, credential.api_key, provider or business, business, credential.id


async def resolve_effective_channel(
    session,
    *,
    user_id: UUID | None = None,
    role_id: str | None = None,
    token: LoginTokenModel | None = None,
) -> tuple[str, str, str, str, UUID | None]:
    """The single funnel: resolve ``(base_url, api_key, provider_model, business_name,
    credential_id)`` for a request or a background job, or a credential-less tuple.

    ``token`` carries the channel pinned at login when present; ``role_id`` is the effective
    role (the user's role, or ``anonymous`` for guests); ``user_id`` (explicit, or taken from
    ``token``) enables the user-level ban gate. The caller does NOT pass any client- or
    payload-supplied base_url/key — those paths are structurally absent from this signature.

    - A pinned channel that is still active and not banned for the user is used directly;
      the model is the role's ``default_model``, else the channel's preferred active route,
      else the first active catalog model.
    - A pinned channel that was disabled (credential-level, or a user-level Tokens ban on
      that key) fails over to another active channel of the same role the user is not banned
      from.
    - No pinned channel (guest, or admin/legacy token) picks fresh from the role; if the role
      has none, the credential-less tuple is returned — every caller MUST fail-fast on it
      (API → 503, worker → NoActiveChannelError); falling back to a global key is forbidden.
    """
    if user_id is None and token is not None:
        user_id = token.user_id
    credential_id = token.credential_id if token is not None else None
    if credential_id is not None:
        if not await user_banned_from(session, user_id, credential_id):
            credential = await session.get(LLMCredentialModel, credential_id)
            if credential is not None and credential.is_active:
                return await channel_route(session, credential, role_id)
        alt = await pick_credential(session, role_id, user_id) if role_id else None
        if alt is not None and alt != credential_id:
            credential = await session.get(LLMCredentialModel, alt)
            if credential is not None:
                return await channel_route(session, credential, role_id)
        business = await fallback_model(session, role_id)
        provider = await provider_model_name(session, business) if business else ""
        return "", "", provider, business, None
    picked = await pick_credential(session, role_id, user_id) if role_id else None
    if picked is not None:
        credential = await session.get(LLMCredentialModel, picked)
        if credential is not None:
            return await channel_route(session, credential, role_id)
    return "", "", "", "", None


async def resolve_chat_route(
    session, token: LoginTokenModel | None, role_id: str
) -> tuple[str, str, str, str, UUID | None]:
    """Web-chat adapter: the login token pins the channel; role comes from the session."""
    return await resolve_effective_channel(session, role_id=role_id, token=token)


async def resolve_channel_for_owner(
    session_factory, user_id: UUID
) -> tuple[str, str, str, str, UUID | None]:
    """Headless adapter (worker jobs / e2e scripts): resolve the OWNER's channel with the
    SAME funnel the web chat uses, with no request token to carry a pin.

    It loads the owner account and applies the checks a ``/chat`` request for that user
    would: the account must exist and be active (``users.is_active``), the effective role
    must be active, and the channel comes from :func:`resolve_effective_channel` itself —
    role-binding / credential ``is_active`` / user-level Tokens-ban validation is
    byte-identical to the web path (the ban gate now also applies to the headless path).

    A credential-less tuple means "no channel": the caller MUST fail the job — never fall
    back to a payload-embedded or process-global key.
    """
    async with session_factory() as session:
        user = await session.get(UserModel, user_id)
        if user is None or not user.is_active:
            return "", "", "", "", None
        role = await get_role(session, user.role_id)
        if role is None or not role.is_active:
            return "", "", "", "", None
        return await resolve_effective_channel(session, user_id=user_id, role_id=user.role_id)
