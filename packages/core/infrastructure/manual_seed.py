"""Boot-time seed of the built-in product usage manual into SQL + pgvector.

The manual (Markdown under ``core/seed/manual/``) answers product-usage
questions ("how do I configure an LLM key / create a research task / change
my password …") through the normal rag_search tool. Each Markdown ``##``
section becomes one leaf chunk prefixed with 【doc · section】, written to the
``chunks`` table as ``source_type='manual'`` rows with ``user_id IS NULL``; the
visibility predicates (``core.infrastructure.visibility``) expose owner-NULL
manual chunks to every tenant and to guests.

Idempotency: a version marker in ``app_settings['manual_seed']`` short-circuits
every boot after the first success; a failed run (e.g. the embedding service is
still starting) leaves the marker untouched and retries on the next boot. Re-
import deletes the previous row set first, so chunk ids never duplicate.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger("uvicorn.error")

# Bump whenever the manual content changes so every deployment re-seeds once.
MANUAL_VERSION = "2026-09-20.2"
MANUAL_SOURCE_TYPE = "manual"
MANUAL_DIR = Path(__file__).resolve().parent.parent / "seed" / "manual"

# A section longer than this is still split (paragraph merge up to the cap) so
# one chunk never swallows several topics.
_SECTION_CAP = 1100


def section_blocks(text: str) -> list[str]:
    """Split a manual page into per-topic chunks, one per ``##`` section.

    Every block is prefixed with 【文档标题 · 小节】, so both the keyword
    (content_search / english tsvector) and the vector channel carry the topic
    context inside the chunk itself — no LLM contextualization at boot.
    """
    from core.infrastructure.ingest import split_chunks

    doc_title = next(
        (ln.lstrip("# ").strip() for ln in text.splitlines() if ln.startswith("# ")),
        "使用手册",
    )
    out: list[str] = []
    for part in re.split(r"\n(?=## )", text):
        part = part.strip()
        if not part:
            continue
        head = next(
            (ln for ln in part.splitlines() if ln.startswith(("## ", "# "))), ""
        )
        section = head.lstrip("# ").strip()
        # A part holding only its heading (the H1 preamble when content starts at
        # the first ``##``) carries no answer text — skip the empty block.
        if part == head:
            continue
        prefix = f"【{doc_title} · {section}】" if section != doc_title else f"【{doc_title}】"
        body = part
        if len(body) > _SECTION_CAP:
            # Split the oversized section on sentence units, prefix every piece.
            for piece in split_chunks(body, 900, 0, "sentence"):
                out.append(f"{prefix}\n{piece}")
        else:
            out.append(f"{prefix}\n{body}")
    return out


def build_manual_chunks(text: str) -> list:
    from core.infrastructure.ingest import Chunk
    from rag.query.cjk import segment

    chunks = []
    for block in section_blocks(text):
        # Manual pages are Chinese-first with English UI labels embedded: index
        # the CJK channel; the english tsvector still sees the ASCII labels.
        chunks.append(Chunk(content_en=block, content_search=segment(block)))
    return chunks


async def seed_product_manual(session_factory, embedder) -> bool:
    """Ingest the manual if the version marker is missing/stale. True = re-seeded."""
    from core.infrastructure.drive_repositories import SqlChunkRepository
    from core.infrastructure.ingest import write_query_repo_chunks
    from core.infrastructure.security import get_setting, set_setting

    async with session_factory() as session:
        marker = await get_setting(session, "manual_seed")
    if marker and marker.get("version") == MANUAL_VERSION:
        return False

    docs = sorted(MANUAL_DIR.glob("*.md"))
    if not docs:
        logger.warning("manual seed: no files under %s — skipping", MANUAL_DIR)
        return False

    repo = SqlChunkRepository(session_factory)
    # Drop the previous generation (stale files included) before writing.
    prev_ids = list(dict.fromkeys(
        [*(marker or {}).get("docs", []), *[f"manual/{p.stem}" for p in docs]]
    ))
    await repo.delete_by_source(MANUAL_SOURCE_TYPE, prev_ids)

    written: list[str] = []
    for path in docs:
        doc_id = f"manual/{path.stem}"
        chunks = build_manual_chunks(path.read_text(encoding="utf-8"))
        for c in chunks:
            c.meta = {"doc": path.stem, "manual_version": MANUAL_VERSION}
        await write_query_repo_chunks(
            session_factory, embedder,
            chunks=chunks, user_id=None,
            source_type=MANUAL_SOURCE_TYPE, source_id=doc_id,
        )
        written.append(doc_id)
        logger.info("manual seed: %s → %d chunks", doc_id, len(chunks))

    async with session_factory() as session:
        await set_setting(session, "manual_seed", {"version": MANUAL_VERSION, "docs": written})
    return True
