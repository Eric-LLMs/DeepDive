"""``read_document``: extract the text content of a document the user attached to the chat.

Attachments ride on the chat payload as a drive ``asset_id``; the agent only sees the
``[Attached: <name> (asset_id <id>)]`` note. Without a reader it can guess but never open
the file, so every "parse this PDF / summarize this Word doc" request fails. This tool
closes that gap: it loads the asset bytes from storage and runs the same extractor the
ingest worker uses — PDF (PyMuPDF body text, tables via the vision LLM), .docx
(python-docx), legacy .doc (antiword), Excel (.xlsx via openpyxl), PowerPoint
(.pptx/.potx/.ppsx slide text + speaker notes via python-pptx), plus plain text /
markdown / csv / json and subtitles. Images are routed to the ``vision`` tool instead,
and the output is capped so one huge document cannot flood the agent's context window.
"""
from __future__ import annotations

import logging
from pathlib import Path
from uuid import UUID

from agent import Context, ToolExecution, ToolOutput, ToolRuntime, define_tool, text_block
from core.infrastructure.drive_repositories import SqlAssetRepository
from core.infrastructure.ingest import UnsupportedFileType, extract_document_text
from core.infrastructure.storage import object_key

log = logging.getLogger(__name__)

# One tool result must not blow up the agent's context window; the tail is dropped with a
# truncation note so the agent can tell the user it saw only the first part.
MAX_OUTPUT_CHARS = 20_000

# Extensions handled by the dedicated vision tool, not by text extraction.
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}


def register(runtime: ToolRuntime, ctx: Context, llm) -> None:
    async def read_document(args: dict, exec: ToolExecution) -> str:
        asset_id = args["asset_id"]
        repo = SqlAssetRepository(ctx.resolve("session_factory"))
        asset = await repo.get(UUID(asset_id))
        if asset is None or not asset.object_sha256:
            raise ValueError(f"asset {asset_id} not found or has no stored object")
        data = await ctx.resolve("storage").get(object_key(asset.object_sha256))
        if data is None:
            raise ValueError(f"object bytes missing for asset {asset_id}")

        name = getattr(asset, "name", None) or "document"
        ext = Path(name).suffix.lower()
        mime = (getattr(asset, "mime_type", None) or "").lower()
        if ext in _IMAGE_EXTS or mime.startswith("image/"):
            return (
                f"'{name}' is an image. Call the `vision` tool with asset_id {asset_id} "
                "to analyze its visual content."
            )
        try:
            text = await extract_document_text(data, name, llm)
        except UnsupportedFileType as exc:
            return (
                f"Cannot extract text from '{name}': {exc} — you must NOT substitute "
                "another document from the conversation for this one; tell the user this "
                "file could not be read."
            )
        if not text.strip():
            return (
                f"'{name}' was parsed but contains no extractable text — it may be a "
                "scanned/image-only document — and you must NOT summarize a different "
                "document in its place."
            )
        if len(text) > MAX_OUTPUT_CHARS:
            total = len(text)
            text = text[:MAX_OUTPUT_CHARS] + f"\n\n[...truncated; {total} chars total]"
        return text

    runtime.register(
        define_tool(
            name="read_document",
            description="Extract the text content of a document the user attached to the "
            "chat (PDF, Word .doc/.docx, Excel .xlsx/.xlsm, PowerPoint .pptx/.potx/.ppsx, "
            "txt/markdown/csv/json, subtitles). Attachments arrive as "
            "[Attached: <filename> (asset_id <id>)]. Whenever the "
            "user asks about the content of an attached document, you MUST call this tool "
            "with that asset_id instead of guessing or claiming the file cannot be read. "
            "For attached images and screenshots use the `vision` tool instead.",
            parameters={
                "type": "object",
                "properties": {
                    "asset_id": {
                        "type": "string",
                        "description": "Cloud-drive asset id of the attached document "
                        "(from the [Attached: ...] note).",
                    },
                },
                "required": ["asset_id"],
            },
            output=ToolOutput(
                schema={"type": "string"}, render=lambda args, value: [text_block(value)]
            ),
            execute=read_document,
        )
    )
