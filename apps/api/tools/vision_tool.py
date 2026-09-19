"""``vision``: read a drive image asset with the user's authorized models.

The agent's main model may be text-only, so it cannot see the screenshots and images
users attach to the chat — it only sees the ``[Attached: name (asset_id)]`` note. This
tool resolves the image bytes from the cloud drive, sends them to the vision-capable
models the requesting user's role is authorized for, and returns the first real analysis
so the agent can discuss the visual content.

Channel selection follows the unified permission funnel
(``core.infrastructure.vision_caption.resolve_vision_channels``): the candidate list is
exclusively the role's database-authorized models — vision-marked names tried first, the
rest as a capability gamble; guests use the anonymous tier under its existing daily
quota; a model-less role downgrades onto that tier and is refused honestly once the
free allowance runs out. The resolution and the ``describe_image`` trial chain live in
``core.infrastructure.vision_caption`` so the RAG ingest worker (image captioning at
index time) can reuse them without importing this module.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from agent import Context, ToolExecution, ToolOutput, ToolRuntime, define_tool, text_block
from core.infrastructure.drive_repositories import SqlAssetRepository
from core.infrastructure.request_context import get_request_user_id
from core.infrastructure.storage import object_key
from core.infrastructure.vision_caption import (
    DEFAULT_PROMPT as _DEFAULT_PROMPT,
    VisionNotAuthorized,
    VisionUnsupported,
    describe_image,
    mime_for as _mime_for,
)


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


def register(runtime: ToolRuntime, ctx: Context, llm) -> None:
    async def vision_analyze(args: dict, exec: ToolExecution) -> str:
        asset_id = args["asset_id"]
        question = (args.get("question") or "").strip()
        asset, data = await _load_asset(asset_id, ctx)
        prompt = f"基于所附图片回答以下问题：{question}" if question else _DEFAULT_PROMPT
        try:
            return await describe_image(
                data,
                _mime_for(asset),
                llm=llm,
                session_factory=ctx.resolve("session_factory"),
                prompt=prompt,
                user_id=get_request_user_id() or asset.user_id,
            )
        except VisionNotAuthorized as exc:
            # Surface the authorization failure honestly — the agent tells the user instead
            # of the tool silently trying another key.
            return f"Vision analysis refused: {exc}"
        except VisionUnsupported as exc:
            return f"Image processing not supported: {exc}"

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
