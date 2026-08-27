"""``doc_outline`` / ``doc_mindmap`` / ``doc_slides``: turn a cloud-drive document into
learning artifacts (Gemini-Notebook-style).

All three tools read the asset's stored bytes via the shared storage + session_factory
provided in ``deps.py`` (same seam as ``pdf_tools``), extract the document text with the
ingest pipeline's ``extract_document_text``, then hand it to the LLM. Outline and mind-map
come back as text rendered in chat; slides build a text-only .pptx that is saved back into
the user's cloud drive as a READY asset.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import tempfile
from datetime import datetime
from pathlib import Path
from uuid import UUID

from agent import Context, ToolExecution, ToolOutput, ToolRuntime, define_tool, text_block
from core.infrastructure import media as media_lib
from core.infrastructure.drive_repositories import (
    SqlAssetRepository,
    SqlGlobalObjectRepository,
)
from core.infrastructure.ingest import extract_document_text
from core.infrastructure.request_context import get_request_user_id
from core.infrastructure.storage import object_key
from api.tools.pdf_tools import _load_asset_bytes

log = logging.getLogger(__name__)

_MAX_TEXT_CHARS = 40_000  # cap the document text handed to the LLM
_PPTX_MIME = (
    "application/vnd.openxmlformats-officedocument.presentationml.presentation"
)

_OUTLINE_SYSTEM = (
    "You are a document structure analyst. Read the document text and produce a "
    "hierarchical outline as Markdown headings (#/##/###). Keep every section "
    "label concise. Output only the outline."
)

_MINDMAP_SYSTEM = (
    "You are a knowledge-structure analyst. Turn the document text into a mind map. "
    "Output an indented text tree: the central topic on the first line, then one "
    "branch per line with leading tabs for depth. Output only the tree."
)

_SLIDES_SYSTEM = (
    "You are a slide deck writer. Turn the document text into a presentation outline. "
    "Output ONLY a JSON array (no prose, no markdown fences) of {count} objects, each "
    "with 'title' (a short slide heading) and 'bullets' (an array of 3-5 concise "
    "bullet points)."
)


async def _load_asset_with_name(asset_id: str, ctx: Context) -> tuple[str, bytes]:
    """Return ``(asset_name, stored_bytes)`` for a drive asset."""
    repo = SqlAssetRepository(ctx.resolve("session_factory"))
    asset = await repo.get(UUID(asset_id))
    name = asset.name if asset is not None else "document"
    data = _load_asset_bytes(asset_id, ctx)
    return name, data


async def _extract(asset_id: str, ctx: Context, llm) -> tuple[str, str]:
    """Pull the asset's text; the second tuple element is '' when nothing was extracted."""
    name, data = await _load_asset_with_name(asset_id, ctx)
    try:
        text = await extract_document_text(data, name, llm)
    except Exception as exc:  # noqa: BLE001 - an unextractable file is a "no text" case
        log.info("no extractable text for %s: %s", name, exc)
        return name, ""
    return name, text.strip()


def _crop(text: str) -> str:
    return text[:_MAX_TEXT_CHARS]


def _parse_slides_json(raw: str) -> list[tuple[str, str]]:
    """Best-effort parse of the LLM's slide-outline JSON into ``(title, bullets)``.

    Tolerates markdown fences, leading prose and trailing text around the array.
    """
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-z]*\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    start = raw.find("[")
    end = raw.rfind("]")
    if start != -1 and end > start:
        raw = raw[start : end + 1]
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning("slide JSON unparseable: %s", exc)
        return []
    if not isinstance(data, list):
        return []
    slides: list[tuple[str, str]] = []
    for item in data:
        if not isinstance(item, dict) or not item.get("title"):
            continue
        bullets = item.get("bullets") or []
        if isinstance(bullets, list):
            bullets = "\n".join(str(b) for b in bullets if str(b).strip())
        slides.append((str(item["title"]), str(bullets)))
    return slides


async def _save_pptx_to_drive(pptx_bytes: bytes, src_name: str, ctx: Context) -> dict:
    """Persist a generated .pptx into the caller's cloud drive as a READY asset."""
    user_id = get_request_user_id()
    if user_id is None:
        raise ValueError("no authenticated user to save the file for")

    stem = Path(src_name).stem or "slides"
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_name = f"{stem}_slides_{ts}.pptx"
    sha = hashlib.sha256(pptx_bytes).hexdigest()

    storage = ctx.resolve("storage")
    session_factory = ctx.resolve("session_factory")
    await storage.put(object_key(sha), pptx_bytes)
    await SqlGlobalObjectRepository(session_factory).upsert_and_increment(
        sha, len(pptx_bytes), object_key(sha), _PPTX_MIME
    )
    asset = await SqlAssetRepository(session_factory).create(
        user_id,
        out_name,
        mime_type=_PPTX_MIME,
        size=len(pptx_bytes),
        object_sha256=sha,
        file_status="READY",
        rag_status="NOT_STARTED",
    )
    return {"asset_id": str(asset.id), "name": out_name}


def register(runtime: ToolRuntime, ctx: Context, llm) -> None:
    async def doc_outline(args: dict, exec: ToolExecution) -> str:
        _, text = await _extract(args["asset_id"], ctx, llm)
        if not text:
            return "No extractable text in this document."
        return await llm.complete(_crop(text), _OUTLINE_SYSTEM)

    async def doc_mindmap(args: dict, exec: ToolExecution) -> str:
        _, text = await _extract(args["asset_id"], ctx, llm)
        if not text:
            return "No extractable text in this document."
        return await llm.complete(_crop(text), _MINDMAP_SYSTEM)

    async def doc_slides(args: dict, exec: ToolExecution) -> str:
        name, text = await _extract(args["asset_id"], ctx, llm)
        if not text:
            return "No extractable text in this document."
        count = max(3, min(int(args.get("count") or 8), 20))
        raw = await llm.complete(
            f"Produce {count} slides:\n\n" + _crop(text),
            _SLIDES_SYSTEM.format(count=count),
        )
        slides = _parse_slides_json(raw)
        if not slides:
            return "Could not parse a slide outline from the model response."

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "slides.pptx"
            media_lib.build_text_pptx(slides, out, title=Path(name).stem or name)
            pptx_bytes = out.read_bytes()

        saved = await _save_pptx_to_drive(pptx_bytes, name, ctx)
        return (
            f"Saved {saved['name']} ({len(slides)} slides) to your cloud drive "
            f"[asset {saved['asset_id']}]."
        )

    runtime.register(
        define_tool(
            name="doc_outline",
            description="Generate a hierarchical Markdown outline (table of contents) "
            "of a cloud-drive document. Returns the outline as text.",
            parameters={
                "type": "object",
                "properties": {
                    "asset_id": {
                        "type": "string",
                        "description": "Cloud-drive asset id of the source document.",
                    },
                },
                "required": ["asset_id"],
            },
            output=ToolOutput(
                schema={"type": "string"}, render=lambda args, value: [text_block(value)]
            ),
            execute=doc_outline,
        )
    )

    runtime.register(
        define_tool(
            name="doc_mindmap",
            description="Generate a mind map of a cloud-drive document as an indented "
            "text tree. Returns the tree as text.",
            parameters={
                "type": "object",
                "properties": {
                    "asset_id": {
                        "type": "string",
                        "description": "Cloud-drive asset id of the source document.",
                    },
                },
                "required": ["asset_id"],
            },
            output=ToolOutput(
                schema={"type": "string"}, render=lambda args, value: [text_block(value)]
            ),
            execute=doc_mindmap,
        )
    )

    runtime.register(
        define_tool(
            name="doc_slides",
            description="Turn a cloud-drive document into a slide deck. Builds a .pptx "
            "and saves it back into the user's cloud drive. Returns the saved file name "
            "and asset id.",
            parameters={
                "type": "object",
                "properties": {
                    "asset_id": {
                        "type": "string",
                        "description": "Cloud-drive asset id of the source document.",
                    },
                    "count": {
                        "type": "integer",
                        "description": "Number of slides to generate (3-20, default 8).",
                    },
                },
                "required": ["asset_id"],
            },
            output=ToolOutput(
                schema={"type": "string"}, render=lambda args, value: [text_block(value)]
            ),
            execute=doc_slides,
        )
    )
