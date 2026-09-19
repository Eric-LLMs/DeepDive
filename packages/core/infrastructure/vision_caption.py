"""Shared vision-model plumbing: channel resolution + one-shot image description.

Both the ``vision`` chat tool (``apps/api/tools/vision_tool.py``) and the RAG ingest
worker (captioning embedded images at index time, ``apps/worker/tasks.py``) need to send
image bytes to the configured vision model. The worker must not import the API tool
module (tool registration side effects), so the reusable half lives here:

- :func:`resolve_vision_channel` — ``(base_url, api_key, provider_model)`` for the model
  chosen via ``tools.vision.model`` in the admin Tools config (empty → first active
  catalog model), following catalog entry → active route → credential.
- :func:`describe_image` — build a ``data:<mime>;base64`` URL (magic-byte sniff as
  fallback so the endpoint never sees an empty MIME) and return the model's text.
"""
from __future__ import annotations

import base64
from typing import Any, Optional

from core.config import get_tool_config
from core.infrastructure.db import (
    CredentialModelModel,
    LLMCredentialModel,
    LLMModelModel,
)
from sqlalchemy import or_, select

# The model sees this prompt when the caller did not pass a specific question.
DEFAULT_PROMPT = (
    "详细分析并描述图片内容，提取其中的关键文字、代码或图表信息，并指出核心要点。"
)

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


async def resolve_vision_channel(session_factory) -> tuple[str, str, str]:
    """Resolve ``(base_url, api_key, provider_model)`` for the configured vision model.

    ``tools.vision.model`` names a catalog display name or provider id; empty means the
    first active catalog model. Returns the credential's base_url/api_key and the catalog
    entry's real provider model id — the tuple ``llm.chat`` needs to route one call.
    """
    display = (get_tool_config("vision").get("model") or "").strip()
    async with session_factory() as session:
        if display:
            catalog = (
                await session.execute(
                    select(LLMModelModel).where(
                        or_(
                            LLMModelModel.name == display,
                            LLMModelModel.provider_model_name == display,
                        )
                    )
                )
            ).scalar_one_or_none()
        else:
            catalog = (
                await session.execute(
                    select(LLMModelModel)
                    .where(LLMModelModel.is_active.is_(True))
                    .order_by(LLMModelModel.created_at)
                    .limit(1)
                )
            ).scalar_one_or_none()
        if catalog is None:
            raise ValueError("no vision model available: Model Catalog is empty or none active")
        route = (
            await session.execute(
                select(CredentialModelModel)
                .where(
                    CredentialModelModel.model_id == catalog.id,
                    CredentialModelModel.is_active.is_(True),
                )
                .order_by(CredentialModelModel.priority)
                .limit(1)
            )
        ).scalar_one_or_none()
        if route is None:
            raise ValueError(f"vision model '{catalog.name}' has no active credential route")
        credential = await session.get(LLMCredentialModel, route.credential_id)
        if credential is None or not credential.is_active:
            raise ValueError(f"vision model '{catalog.name}' credential is inactive")
        return credential.base_url, credential.api_key, catalog.provider_model_name or catalog.name


async def describe_image(
    data: bytes,
    mime: str = "",
    *,
    llm,
    session_factory,
    prompt: Optional[str] = None,
) -> str:
    """Send one image to the vision model and return its text analysis."""
    url = data_url(data, mime)
    base_url, api_key, provider_model = await resolve_vision_channel(session_factory)
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
    return (resp.get("content") or "").strip()
