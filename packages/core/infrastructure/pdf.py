"""PDF text extraction: body text via PyMuPDF + table detection → image → vision LLM.

The strategy mirrors the LLMs-Lab/RAG reference (table → image → read → text) but drops
the torch/Table-Transformer detector: PyMuPDF's built-in ``page.find_tables()`` locates
table bounding boxes with no ML stack, and DeepDive's existing chat LLM (default
``gpt-4o-mini``) reads the rendered table image via an OpenAI ``image_url`` content part.

Degradation: a single table transcription failure logs and skips that table — the ingest
job must never fail because one table could not be read (same philosophy as
``contextualize_chunks``). If the pinned upstream model is not vision-capable, every
table falls back to the body text alone and the file still indexes.
"""
from __future__ import annotations

import asyncio
import base64
import logging

import pymupdf  # PyMuPDF (the ``fitz`` name is a deprecated alias)

log = logging.getLogger(__name__)

MAX_TABLES = 20


def extract_pdf_text(content: bytes) -> str:
    """Return the plain-text body of a PDF, pages newline-joined."""
    doc = pymupdf.open(stream=content, filetype="pdf")
    try:
        return "\n".join(doc[i].get_text("text") for i in range(doc.page_count))
    finally:
        doc.close()


def detect_tables(content: bytes, max_tables: int = MAX_TABLES) -> list[bytes]:
    """Render each detected table region to a PNG (bytes) for the vision LLM.

    A page whose table detection raises is skipped entirely (degrade, never crash); the
    total is capped at ``max_tables`` so a pathological PDF cannot fan out too many vision
    calls.
    """
    doc = pymupdf.open(stream=content, filetype="pdf")
    try:
        images: list[bytes] = []
        for i in range(doc.page_count):
            page = doc[i]
            try:
                tables = page.find_tables()
            except Exception:  # noqa: BLE001 - detection hiccup on one page
                continue
            for table in tables.tables:
                if len(images) >= max_tables:
                    return images
                pix = page.get_pixmap(clip=table.bbox)
                images.append(pix.tobytes("png"))
    finally:
        doc.close()
    return images


def _data_url(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


async def pdf_table_to_text(png: bytes, llm, prompt: str | None = None) -> str:
    """Ask the vision LLM to transcribe one table image into text."""
    prompt = prompt or (
        "Transcribe the table in this image to text. Preserve rows and columns, keep "
        "numbers and values exact, and output only the transcribed text."
    )
    resp = await llm.chat(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": _data_url(png)}},
                ],
            }
        ]
    )
    return (resp.get("content") or "").strip()


async def extract_pdf_document(
    content: bytes, llm, *, max_concurrency: int = 4, max_tables: int = MAX_TABLES
) -> str:
    """Body text + transcribed tables, joined into one document text for chunking."""
    body = extract_pdf_text(content)
    tables = detect_tables(content, max_tables=max_tables)
    if not tables:
        return body

    sem = asyncio.Semaphore(max_concurrency)

    async def one(png: bytes) -> str:
        async with sem:
            try:
                return await pdf_table_to_text(png, llm)
            except Exception as exc:  # noqa: BLE001 - skip a table, never fail ingest
                log.warning("pdf table transcription failed, skipped: %s", exc)
                return ""

    transcribed = await asyncio.gather(*(one(p) for p in tables))
    parts = [body] + [t for t in transcribed if t]
    return "\n\n".join(parts)
