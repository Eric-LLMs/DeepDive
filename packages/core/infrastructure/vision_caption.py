"""Shared vision-model plumbing: channel resolution + one-shot image description.

Both the ``vision`` chat tool (``apps/api/tools/vision_tool.py``) and the RAG ingest
worker (captioning embedded images at index time, ``apps/worker/tasks.py``) need to send
image bytes to the configured vision model. The worker must not import the API tool
module (tool registration side effects), so the reusable half lives here:

- :func:`resolve_vision_channels` — the ordered ``(base_url, api_key, provider_model)``
  trial chain resolved INSIDE the unified permission funnel (same gates as
  ``llm_routing.resolve_effective_channel``: role-bound active credentials minus user-level
  bans, vision-named entries first, no unbound-key fallback ever).
- :func:`describe_image` — build a ``data:<mime>;base64`` URL (magic-byte sniff as
  fallback so the endpoint never sees an empty MIME), walk the chain until a model answers,
  and raise :class:`VisionUnsupported` when none can.
"""
from __future__ import annotations

import base64
from typing import Any, Optional
from uuid import UUID

from core.config import get_tool_config
from core.infrastructure.db import (
    AccessTokenModel,
    CredentialModelModel,
    LLMCredentialModel,
    LLMModelModel,
    RoleCredentialModel,
    UserModel,
)
from sqlalchemy import select

# The model sees this prompt when the caller did not pass a specific question.
DEFAULT_PROMPT = (
    "详细分析并描述图片内容，提取其中的关键文字、代码或图表信息，并指出核心要点。"
)


class VisionNotAuthorized(RuntimeError):
    """No authorized vision channel exists — surfaced honestly, never key-bypassed."""


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


async def resolve_vision_channels(
    session_factory, *, user_id: UUID | None = None, role_id: str | None = None
) -> list[tuple[str, str, str]]:
    """Resolve the ordered ``(base_url, api_key, provider_model)`` trial chain INSIDE the funnel.

    Same gates as ``llm_routing.resolve_effective_channel``: only credentials bound to the
    requesting role (``role_credentials`` ∧ both ``is_active``) with the user-level Tokens
    ban excluded are candidates — there is deliberately NO fallback to an unbound/global
    key (doctrine: 杜绝私自兜底取未绑定的 key). Guests/anonymous users are no exception:
    they resolve through their (guest) role, and the platform-wide per-role request quota
    in ``security.authorize_usage`` already meters every chat call — vision adds no second
    counter. Selection among the authorized candidates:

    1. ``tools.vision.model`` (admin Tools config) when set — it must name a catalog entry
       (display name or provider id) actually served by an authorized channel, else
       :class:`VisionNotAuthorized`. The chain is that one entry only.
    2. Otherwise every authorized catalog entry, ordered so that entries whose name or
       provider id contains ``vision`` come first (the name heuristic the operator relies
       on to mark image-capable models), the rest after them as a capability gamble —
       models judge their own image support, so a plain model may still answer on images.
    3. No authorized channel at all → :class:`VisionNotAuthorized`; the caller surfaces it
       honestly (chat tool → visible refusal, worker captioning → skip).

    ``user_id`` (chat request via ``request_context``, or asset owner for headless jobs)
    supplies the role when ``role_id`` is not given directly.
    """
    if role_id is None:
        if user_id is None:
            raise VisionNotAuthorized(
                "vision: no user identity for channel resolution — the vision call is not "
                "bound to a role and no unauthorized fallback is allowed"
            )
        async with session_factory() as session:
            user = await session.get(UserModel, user_id)
        if user is None or not user.is_active:
            raise VisionNotAuthorized("vision: requesting user is missing or inactive")
        role_id = user.role_id

    display = (get_tool_config("vision").get("model") or "").strip()
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
            raise VisionNotAuthorized(
                f"vision: role '{role_id}' has no authorized LLM channel to serve images"
            )
        # (catalog, credential) tuples across every authorized channel, priority-ordered.
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
        pairs = [(cred, model) for model, cred in rows]
    chain: list[tuple[str, str, str]] = []
    for cred, model in rank_vision_entries(pairs, configured=display, role_id=role_id):
        entry = (cred.base_url, cred.api_key, model.provider_model_name or model.name)
        if entry not in chain:  # same model behind two routes is tried once
            chain.append(entry)
    return chain


def rank_vision_entries(
    pairs: list[tuple[LLMCredentialModel, LLMModelModel]], *, configured: str, role_id: str
) -> list[tuple[LLMCredentialModel, LLMModelModel]]:
    """Pure ordering over already-authorized ``(credential, catalog)`` pairs.

    ``configured`` (``tools.vision.model``) must name an authorized entry, else
    :class:`VisionNotAuthorized` — an operator-set model that the role cannot reach is an
    authorization failure, not a cue to bypass the funnel. Without it the whole authorized
    list comes back priority-ordered with ``vision``-named entries (display name or
    provider id, case-insensitive) floated to the front: try those first, then "even a dead
    horse may act as a live one" — gamble the remaining models in order. An empty list is
    an explicit error; the caller never reaches an unbound key.
    """
    if configured:
        low = configured.lower()
        hits = [
            (cred, model)
            for cred, model in pairs
            if model.name.lower() == low or (model.provider_model_name or "").lower() == low
        ]
        if not hits:
            raise VisionNotAuthorized(
                f"vision: configured model '{configured}' is not served by any channel "
                f"authorized for role '{role_id}' — bind it in the admin console or leave "
                "the name empty for auto-selection"
            )
        return hits
    def is_vision(pair) -> bool:
        _cred, model = pair
        return "vision" in model.name.lower() or "vision" in (model.provider_model_name or "").lower()

    ranked = [p for p in pairs if is_vision(p)] + [p for p in pairs if not is_vision(p)]
    if not ranked:
        raise VisionNotAuthorized(
            f"vision: no model is available on channels authorized for role '{role_id}'"
        )
    return ranked


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

    The channel chain comes from :func:`resolve_vision_channels`, so every caller must
    supply a ``user_id`` or ``role_id`` — anonymous vision calls are refused by design.
    Each ``(base_url, api_key, model)`` is tried in order (vision-named first); a call that
    errors or answers with empty text moves to the next candidate. When the whole chain
    fails :class:`VisionUnsupported` is raised — the honest "does not support image
    processing" end state; there is never an unbound-key fallback.
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
