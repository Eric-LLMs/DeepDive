"""``pdf_extract_text`` / ``pdf_table_to_text``: pull searchable text out of drive PDFs.

Body text comes from PyMuPDF; tables are located by ``page.find_tables()``, rendered to
images, and transcribed by the vision LLM — the table → image → read → text strategy from
the LLMs-Lab/RAG reference, minus the torch table-transformer. Both tools read the asset's
stored bytes via the shared storage + session_factory provided in ``deps.py``, so an agent
can inspect a drive PDF that the ingest worker would otherwise handle automatically.
"""
from __future__ import annotations

import asyncio
import logging
from uuid import UUID

from agent import Context, ToolExecution, ToolOutput, ToolRuntime, define_tool, text_block
from core.infrastructure import pdf as pdf_lib
from core.infrastructure.drive_repositories import SqlAssetRepository
from core.infrastructure.storage import get_storage, object_key

log = logging.getLogger(__name__)

_TABLE_PROMPT = (
    "Transcribe the table in this image to text. Preserve rows and columns, keep numbers "
    "and values exact, and output only the transcribed text."
)


async def _load_asset_bytes(asset_id: str, ctx: Context) -> bytes:
    """Fetch a drive asset's stored object bytes by ``asset_id``."""
    repo = SqlAssetRepository(ctx.resolve("session_factory"))
    asset = await repo.get(UUID(asset_id))
    if asset is None or not asset.object_sha256:
        raise ValueError(f"asset {asset_id} not found or has no stored object")
    data = await ctx.resolve("storage").get(object_key(asset.object_sha256))
    if data is None:
        raise ValueError(f"object bytes missing for asset {asset_id}")
    return data


def register(runtime: ToolRuntime, ctx: Context, llm) -> None:
    async def pdf_extract_text(args: dict, exec: ToolExecution) -> str:
        data = await _load_asset_bytes(args["asset_id"], ctx)
        # fitz is CPU-bound; run it off the event loop.
        return await asyncio.to_thread(pdf_lib.extract_pdf_text, data)

    async def pdf_table_to_text(args: dict, exec: ToolExecution) -> str:
        data = await _load_asset_bytes(args["asset_id"], ctx)
        tables = await asyncio.to_thread(pdf_lib.detect_tables, data)
        if not tables:
            return "No tables detected in this PDF."

        sem = asyncio.Semaphore(4)

        async def one(png: bytes) -> str:
            async with sem:
                try:
                    return await pdf_lib.pdf_table_to_text(png, llm, _TABLE_PROMPT)
                except Exception as exc:  # noqa: BLE001 - skip a table, never error out
                    log.warning("pdf table transcription failed, skipped: %s", exc)
                    return ""

        transcribed = await asyncio.gather(*(one(p) for p in tables))
        text = "\n\n".join(t for t in transcribed if t)
        return text or "Table transcription returned no text."

    runtime.register(
        define_tool(
            name="pdf_extract_text",
            description="Extract the plain-text body of a PDF from the cloud drive. "
            "Returns the page text (tables excluded; use pdf_table_to_text for those).",
            parameters={
                "type": "object",
                "properties": {
                    "asset_id": {
                        "type": "string",
                        "description": "Cloud-drive asset id of the PDF.",
                    },
                },
                "required": ["asset_id"],
            },
            output=ToolOutput(
                schema={"type": "string"}, render=lambda args, value: [text_block(value)]
            ),
            execute=pdf_extract_text,
        )
    )

    runtime.register(
        define_tool(
            name="pdf_table_to_text",
            description="Detect tables in a cloud-drive PDF, render each table to an image, "
            "and transcribe it to text with a vision LLM. Returns the table text.",
            parameters={
                "type": "object",
                "properties": {
                    "asset_id": {
                        "type": "string",
                        "description": "Cloud-drive asset id of the PDF.",
                    },
                },
                "required": ["asset_id"],
            },
            output=ToolOutput(
                schema={"type": "string"}, render=lambda args, value: [text_block(value)]
            ),
            execute=pdf_table_to_text,
        )
    )
