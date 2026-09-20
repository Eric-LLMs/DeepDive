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
Legacy .doc additionally gets an embedded-image recovery pass: antiword can only leave a
``[pic]`` placeholder, so ``scan_doc_images`` recovers the actual inline pictures, saves
them as derived drive assets (same ``RAG 图片/<doc>`` folder and dedupe the ingest worker
uses), and lists their asset_ids in the tool output so the agent can feed them to
``vision``.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from pathlib import Path
from uuid import UUID

from agent import Context, ToolExecution, ToolOutput, ToolRuntime, define_tool, text_block
from core.application.drive_service import DriveService
from core.infrastructure.doc_images import scan_doc_images
from core.infrastructure.drive_repositories import SqlAssetRepository
from core.infrastructure.ingest import UnsupportedFileType, extract_document_text
from core.infrastructure.storage import object_key

log = logging.getLogger(__name__)

# One tool result must not blow up the agent's context window; the tail is dropped with a
# truncation note so the agent can tell the user it saw only the first part.
MAX_OUTPUT_CHARS = 20_000

# Extensions handled by the dedicated vision tool, not by text extraction.
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}

# Characters that would turn a doc title into nested folders / broken names.
_FOLDER_BAD = re.compile(r'[\\/:*?"<>|]')


async def _mint_doc_images(data: bytes, asset, ctx: Context) -> str:
    """Save a legacy .doc's inline pictures as drive assets; return a footer listing ids.

    Dedupes on ``(source_asset_id, sha256)`` — an image already extracted by the RAG
    worker (or a previous chat turn) resolves to the existing asset instead of a
    duplicate row. Any failure degrades to the plain text result: minting must never
    break ``read_document``.
    """
    try:
        images, skipped = await asyncio.to_thread(scan_doc_images, data)
        if not images:
            if skipped:
                return (
                    f"\n\n[{skipped} embedded metafile image(s) could not be rendered "
                    "on this server; the [pic] placeholder above is all there is.]"
                )
            return ""
        stem = _FOLDER_BAD.sub("_", Path(asset.name or "doc").stem).strip(". ") or "doc"
        folder = f"RAG 图片/{stem}"
        drive = DriveService(ctx.resolve("session_factory"))
        ids: list[str] = []
        for img in images:
            digest = hashlib.sha256(img["data"]).hexdigest()
            existing = await drive.assets.get_by_source_content(asset.id, digest)
            if existing is not None:
                ids.append(str(existing.id))
                continue
            saved = await drive.save_artifact(
                asset.user_id,
                img["name"],
                img["mime"],
                img["data"],
                folder_path=folder,
                workspace_id=asset.workspace_id,
                source_asset_id=asset.id,
            )
            ids.append(str(saved.id))
        lines = "\n".join(
            f"- doc_image {i + 1}: asset_id {aid}" for i, aid in enumerate(ids)
        )
        extra = f" ({skipped} metafile image(s) not renderable)" if skipped else ""
        return (
            f"\n\n[This .doc contains {len(ids)} embedded image(s){extra}. They are saved "
            "in the cloud drive; call the `vision` tool with an asset_id below to analyze "
            f"the picture or table it shows:\n{lines}]"
        )
    except Exception as exc:  # noqa: BLE001 — image recovery is best-effort
        log.warning("doc image minting failed for asset %s: %s", asset.id, exc)
        return ""


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
        footer = ""
        if ext == ".doc":
            footer = await _mint_doc_images(data, asset, ctx)
        if not text.strip():
            return (
                f"'{name}' was parsed but contains no extractable text — it may be a "
                "scanned/image-only document — and you must NOT summarize a different "
                "document in its place." + footer
            )
        if len(text) > MAX_OUTPUT_CHARS:
            total = len(text)
            text = text[:MAX_OUTPUT_CHARS] + f"\n\n[...truncated; {total} chars total]"
        return text + footer

    runtime.register(
        define_tool(
            name="read_document",
            description="Extract the text content of a document the user attached to the "
            "chat (PDF, Word .doc/.docx, Excel .xlsx/.xlsm, PowerPoint .pptx/.potx/.ppsx, "
            "txt/markdown/csv/json, subtitles). Attachments arrive as "
            "[Attached: <filename> (asset_id <id>)]. Whenever the "
            "user asks about the content of an attached document, you MUST call this tool "
            "with that asset_id instead of guessing or claiming the file cannot be read. "
            "Embedded pictures in a legacy .doc come back with their own asset_ids at the "
            "end of the result — pass those ids to the `vision` tool to read the image or "
            "table. For attached images and screenshots use the `vision` tool instead.",
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
