"""``vision``: read a drive image asset with the configured vision model.

The agent's main model is text-only (DeepSeek), so it cannot see the screenshots and
images users attach to the chat — it only sees the ``[Attached: name (asset_id)]`` note.
This tool resolves the image bytes from the cloud drive, sends them to the vision model
configured in the admin console's Model Catalog, and returns the model's analysis so the
agent can discuss the visual content.

The vision model's serving channel is resolved the same way the chat router does it
(``_shared._channel_route``): a catalog entry (``LLMModelModel``) → its active route
(``CredentialModelModel``) → the credential (``LLMCredentialModel``). Which catalog model
is used is set via ``tools.vision.model`` in the admin console's Tools config; when empty,
the first active catalog model is the default.
"""
from __future__ import annotations

import base64
from typing import Any
from uuid import UUID

from agent import Context, ToolExecution, ToolOutput, ToolRuntime, define_tool, text_block
from core.config import get_tool_config
from core.infrastructure.db import (
    CredentialModelModel,
    LLMCredentialModel,
    LLMModelModel,
)
from core.infrastructure.drive_repositories import SqlAssetRepository
from core.infrastructure.storage import object_key
from sqlalchemy import or_, select

# The model sees this prompt when the user did not pass a specific question.
_DEFAULT_PROMPT = (
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


def _sniff_mime(data: bytes) -> str:
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


def _mime_for(asset: Any) -> str:
    """Prefer the stored mime_type, else the file extension; empty means 'sniff'."""
    mime = getattr(asset, "mime_type", None)
    if mime:
        return mime
    name = getattr(asset, "name", "") or ""
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return _EXT_MIME.get(ext, "")


def _data_url(data: bytes, asset: Any) -> str:
    """Build a ``data:<mime>;base64,...`` URL, never an empty-MIME ``data:;...``.

    The MIME chain (stored type → extension → magic bytes → png fallback) guarantees a
    concrete type, so the vision endpoint never rejects the data URL with a 400.
    """
    mime = _mime_for(asset) or _sniff_mime(data)
    return "data:" + mime + ";base64," + base64.b64encode(data).decode("ascii")


async def _load_asset(asset_id: str, ctx: Context) -> tuple[Any, bytes]:
    """Fetch a drive asset's stored bytes plus its row (for the MIME hint)."""
    repo = SqlAssetRepository(ctx.resolve("session_factory"))
    asset = await repo.get(UUID(asset_id))
    if asset is None or not asset.object_sha256:
        raise ValueError(f"asset {asset_id} not found or has no stored object")
    data = await ctx.resolve("storage").get(object_key(asset.object_sha256))
    if data is None:
        raise ValueError(f"object bytes missing for asset {asset_id}")
    return asset, data


async def _resolve_vision_channel(session_factory) -> tuple[str, str, str]:
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


def register(runtime: ToolRuntime, ctx: Context, llm) -> None:
    async def vision_analyze(args: dict, exec: ToolExecution) -> str:
        asset_id = args["asset_id"]
        question = (args.get("question") or "").strip()
        asset, data = await _load_asset(asset_id, ctx)
        url = _data_url(data, asset)
        prompt = f"基于所附图片回答以下问题：{question}" if question else _DEFAULT_PROMPT
        base_url, api_key, provider_model = await _resolve_vision_channel(
            ctx.resolve("session_factory")
        )
        resp = await llm.chat(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": url}},
                    ],
                }
            ],
            model=provider_model,
            base_url=base_url,
            api_key=api_key,
        )
        return (resp.get("content") or "").strip()

    runtime.register(
        define_tool(
            name="vision",
            description="Analyze an image or screenshot that the user attached to the chat. "
            "Attachments arrive as [Attached: <filename> (<asset_id>)]. Whenever the user "
            "references an attached image or screenshot, you MUST call this tool with that "
            "asset_id to inspect the visual content before answering — even if the filename "
            "seems descriptive. The configured vision model reads the image and returns its "
            "analysis, or answers the user's question about it. Pass the user's specific "
            "question via `question` when they ask about a detail.",
            parameters={
                "type": "object",
                "properties": {
                    "asset_id": {
                        "type": "string",
                        "description": "Cloud-drive asset id of the attached image or "
                        "screenshot (from the [Attached: ...] note).",
                    },
                    "question": {
                        "type": "string",
                        "description": "Optional question about the image. Omit to get a "
                        "general analysis of the image contents.",
                    },
                },
                "required": ["asset_id"],
            },
            output=ToolOutput(
                schema={"type": "string"}, render=lambda args, value: [text_block(value)]
            ),
            execute=vision_analyze,
        )
    )
