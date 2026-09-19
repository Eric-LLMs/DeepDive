"""Shared vision-model plumbing: authorized-channel resolution + image description.

Both the ``vision`` chat tool (``apps/api/tools/vision_tool.py``) and the RAG ingest
worker (captioning embedded images at index time, ``apps/worker/tasks.py``) send image
bytes to LLM models. The worker must not import the API tool module (tool registration
side effects), so the reusable half lives here.

Routing rules (the model source is ONLY the caller's database authorization list — no
global/config key is ever consulted):

1. Guests (no logged-in identity) resolve through the ``anonymous`` role, whose per-day
   request quota is already enforced by ``security.authorize_usage`` at the chat entry —
   vision adds no second counter. A logged-in user whose role holds no model is DOWNGRADED
   onto the anonymous tier: the call consumes that user's guest/free daily allowance
   (same counters and limits as real guests — no second wheel), and once the allowance is
   exhausted the refusal reads "Free trial allowance exhausted — your role has no model
   assigned yet; upgrade or contact the administrator", never a misleading
   "you are an unauthenticated guest" wording.
2. Among the role's authorized models, entries whose name carries a vision marker
   (``vision`` / ``multimodal`` / ``4o`` / ``vl`` …, case-insensitive) are tried first;
   the remaining authorized models follow in order as a capability gamble — models judge
   their own image support and answer "can't process images" themselves, no local
   hard-block.
3. If every candidate fails, :class:`VisionUnsupported` surfaces and the caller prints
   the friendly "image processing is not supported".

- :func:`resolve_vision_channels` — the ordered ``(base_url, api_key, provider_model)``
  trial chain produced by rules 1–2.
- :func:`describe_image` — build a ``data:<mime>;base64`` URL (magic-byte sniff as
  fallback so the endpoint never sees an empty MIME), walk the chain until a model
  answers, and raise :class:`VisionUnsupported` when none can.
"""
from __future__ import annotations

import base64
from typing import Any, Optional
from uuid import UUID

from core.infrastructure.db import (
    AccessTokenModel,
    CredentialModelModel,
    LLMCredentialModel,
    LLMModelModel,
    RoleCredentialModel,
    UserModel,
)
from core.infrastructure.security import authorize_usage, get_role
from fastapi import HTTPException
from sqlalchemy import select

# The model sees this prompt when the caller did not pass a specific question.
DEFAULT_PROMPT = (
    "详细分析并描述图片内容，提取其中的关键文字、代码或图表信息，并指出核心要点。"
)

# Role id whose channel serves unauthenticated guests (quota-metered at the chat entry).
ANONYMOUS_ROLE = "anonymous"

# Substrings (lowercase) that mark a catalog entry as explicitly vision-capable; tried first.
_VISION_HINTS = ("vision", "multimodal", "multi-modal", "4o", "vl", "多模态")


class VisionNotAuthorized(RuntimeError):
    """The role holds no authorized model — surfaced honestly, never key-bypassed."""


class VisionUnsupported(RuntimeError):
    """Every authorized model was tried and none could process the image."""


_EXT_MIME = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "bmp": "image/bmp",
    "svg": "image/svg+xml",
}


def sniff_mime(data: bytes) -> str:
    """Magic-byte sniff for the common image formats; png is the safe fallback."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"BM"):
        return "image/bmp"
    return "image/png"


def mime_for(asset: Any) -> str:
    """Prefer the stored mime_type, else the file extension; empty means 'sniff'."""
    mime = getattr(asset, "mime_type", None)
    if mime:
        return mime
    name = getattr(asset, "name", "") or ""
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return _EXT_MIME.get(ext, "")


def data_url(data: bytes, mime: str = "") -> str:
    """Build a ``data:<mime>;base64,...`` URL, never an empty-MIME ``data:;...``.

    The MIME chain (explicit hint → magic bytes → png fallback) guarantees a concrete
    type, so the vision endpoint never rejects the data URL with a 400.
    """
    return "data:" + (mime or sniff_mime(data)) + ";base64," + base64.b64encode(data).decode(
        "ascii"
    )


def _is_vision_named(model: LLMModelModel) -> bool:
    """True when display name or provider id carries a vision-capability marker."""
    text = f"{model.name} {model.provider_model_name or ''}".lower()
    return any(h in text for h in _VISION_HINTS)


def rank_vision_entries(
    pairs: list[tuple[LLMCredentialModel, LLMModelModel]],
) -> list[tuple[LLMCredentialModel, LLMModelModel]]:
    """Order already-authorized ``(credential, catalog)`` pairs for the trial chain.

    Vision-marked names first (the operator's naming convention is the only capability
    signal consulted), the remaining models after them as a capability gamble — a model
    that cannot see images says so at call time and the chain simply moves on.
    """
    return [p for p in pairs if _is_vision_named(p[1])] + [
        p for p in pairs if not _is_vision_named(p[1])
    ]


async def _authorized_pairs(
    session_factory, *, role_id: str, user_id: Optional[UUID]
) -> list[tuple[LLMCredentialModel, LLMModelModel]]:
    """The role's usable ``(credential, catalog)`` pairs, in route-priority order.

    Same gates as the chat funnel: ``role_credentials`` ∧ both rows active, user-level
    token bans excluded; every mounted catalog model of a passing credential is a
    candidate. An empty list means the role holds nothing — the caller decides whether
    that is the guest tier's channel pool or an explicit refusal.
    """
    async with session_factory() as session:
        cred_rows = (
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
        banned: set[UUID] = set()
        if user_id is not None and cred_rows:
            banned = set(
                (
                    await session.execute(
                        select(AccessTokenModel.credential_id)
                        .where(
                            AccessTokenModel.user_id == user_id,
                            AccessTokenModel.credential_id.is_not(None),
                            AccessTokenModel.is_active.is_(False),
                            AccessTokenModel.credential_id.in_(cred_rows),
                        )
                    )
                ).scalars().all()
            )
        allowed = [c for c in cred_rows if c not in banned]
        if not allowed:
            return []
        rows = (
            await session.execute(
                select(LLMModelModel, LLMCredentialModel)
                .join(CredentialModelModel, CredentialModelModel.model_id == LLMModelModel.id)
                .join(LLMCredentialModel, LLMCredentialModel.id == CredentialModelModel.credential_id)
                .where(
                    CredentialModelModel.credential_id.in_(allowed),
                    CredentialModelModel.is_active.is_(True),
                    LLMModelModel.is_active.is_(True),
                )
                .order_by(CredentialModelModel.priority, LLMModelModel.created_at)
            )
        ).all()
        return [(cred, model) for model, cred in rows]


async def resolve_vision_channels(
    session_factory, *, user_id: Optional[UUID] = None, role_id: Optional[str] = None
) -> list[tuple[str, str, str]]:
    """The ordered ``(base_url, api_key, provider_model)`` trial chain for one vision call.

    ``user_id`` is the chat requester (via ``request_context``) or the document owner on
    headless jobs; ``role_id`` overrides the lookup when the caller already knows it.
    With neither identity the call is a guest and resolves through :data:`ANONYMOUS_ROLE`
    (daily quota already meters the surrounding chat request — over the limit the request
    never reaches this code). A logged-in identity whose role holds no model is downgraded
    onto the anonymous tier, metered against the user's guest/free daily allowance; when
    that allowance is gone the refusal is explicit and no global key is consulted.
    """
    if role_id is None:
        if user_id is None:
            role_id = ANONYMOUS_ROLE
        else:
            async with session_factory() as session:
                user = await session.get(UserModel, user_id)
            if user is None or not user.is_active:
                raise VisionNotAuthorized("vision: requesting user is missing or inactive")
            role_id = user.role_id
    pairs = await _authorized_pairs(session_factory, role_id=role_id, user_id=user_id)
    if not pairs and role_id != ANONYMOUS_ROLE:
        # Model-less role → handle as an anonymous user: consume the user's guest/free
        # daily allowance through the SAME authorize_usage path (counters + limits as real
        # guests), then serve on the anonymous tier's authorized channels.
        async with session_factory() as session:
            anon = await get_role(session, ANONYMOUS_ROLE)
            if anon is not None and user_id is not None:
                try:
                    await authorize_usage(session, user_id, anon)
                except HTTPException as exc:
                    if exc.status_code == 402:
                        raise VisionNotAuthorized(
                            "vision: free trial allowance exhausted — your role has no "
                            "model assigned yet; upgrade or contact the administrator"
                        ) from exc
                    raise
        role_id = ANONYMOUS_ROLE
        pairs = await _authorized_pairs(session_factory, role_id=role_id, user_id=user_id)
    if not pairs:
        raise VisionNotAuthorized(
            f"vision: no model is authorized for role '{role_id}' — contact the "
            "administrator or upgrade the account"
        )
    chain: list[tuple[str, str, str]] = []
    for cred, model in rank_vision_entries(pairs):
        entry = (cred.base_url, cred.api_key, model.provider_model_name or model.name)
        if entry not in chain:  # same model behind two routes is tried once
            chain.append(entry)
    return chain


async def describe_image(
    data: bytes,
    mime: str = "",
    *,
    llm,
    session_factory,
    prompt: Optional[str] = None,
    user_id: Optional[UUID] = None,
    role_id: Optional[str] = None,
) -> str:
    """Send one image down the authorized trial chain and return the first real answer.

    Each ``(base_url, api_key, model)`` is tried in order (vision-marked names first); a
    call that errors or answers with empty text moves to the next candidate — no local
    hard-block, the model's own response decides. When the whole chain fails
    :class:`VisionUnsupported` is raised; callers print the friendly "image processing is
    not supported" (chat) or skip the image (ingest captioning).
    """
    url = data_url(data, mime)
    chain = await resolve_vision_channels(
        session_factory, user_id=user_id, role_id=role_id
    )
    errors: list[str] = []
    for base_url, api_key, provider_model in chain:
        try:
            resp = await llm.chat(
                [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt or DEFAULT_PROMPT},
                            {"type": "image_url", "image_url": {"url": url}},
                        ],
                    }
                ],
                model=provider_model,
                base_url=base_url,
                api_key=api_key,
            )
            text = (resp.get("content") or "").strip()
        except Exception as exc:  # noqa: BLE001 — next authorized candidate may still work
            text = ""
            errors.append(f"{provider_model}: {exc}")
        if text:
            return text
    raise VisionUnsupported(
        "vision: no authorized model could process the image"
        + (f" — {'; '.join(errors)}" if errors else "")
    )
