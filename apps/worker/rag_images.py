"""Extract embedded images from a RAG-ingested document and save them as cloud-drive assets.

The agent's vision tool reads images by ``asset_id``, so a document's figures only become
usable during RAG retrieval if they are persisted as drive files AND referenced from the
chunks that discuss them. This module does the "save + reference" half:

- ``scan_embedded_images`` walks the document and returns, per anchor, the images embedded
  there. Anchors are 1-based page numbers for PDF (via PyMuPDF ``get_images`` /
  ``extract_image``) and for PPTX decks (1-based slide numbers via python-pptx picture
  shapes, riding the same ``[[PAGE:n]]`` axis), and paragraph indexes for DOCX (via the
  ``a:blip r:embed`` drawing anchors) and legacy DOC (via :mod:`core.infrastructure.doc_images`
  magic-header recovery, anchored to the ``[pic]`` placeholders antiword leaves). DOCX has no native pages, so paragraph anchoring is
  the closest equivalent to "the image that travels with this chunk".
- ``save_images`` persists each image with :meth:`DriveService.save_artifact` into a
  dedicated ``RAG 图片/<doc>`` folder, records ``source_asset_id`` (the PDF/DOCX asset) on
  the image asset, and dedupes by ``(source_asset_id, content-hash)`` so re-ingesting the
  same document reuses existing image assets instead of duplicating rows.

Chunk association happens in ``apps/worker/tasks.py`` via ``build_chunks(on_split=...)``:
the ``[[PAGE:n]]`` / ``[[PARA:n]]`` markers in the extracted text are stripped there and
replaced with ``meta["pages"]`` / ``meta["paras"]`` + ``meta["image_ids"]`` (a deduped union
across every page/paragraph the chunk covers).
"""
from __future__ import annotations

import hashlib
import io
import logging
import re

log = logging.getLogger(__name__)

_RASTER_EXTS = {"png", "jpg", "jpeg", "gif", "webp", "bmp"}
_EXT_MIME = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "bmp": "image/bmp",
}

# Characters that would turn a doc title into nested folders / broken names.
_FOLDER_BAD = re.compile(r'[\\/:*?"<>|]')


def _sanitize_folder(name: str) -> str:
    cleaned = _FOLDER_BAD.sub("_", (name or "doc").strip()).strip(". ")
    return cleaned or "doc"


def _scan_pdf(data: bytes) -> dict[int, list[dict]]:
    """Map 1-based page number → embedded raster images of that page."""
    import pymupdf

    out: dict[int, list[dict]] = {}
    doc = pymupdf.open(stream=data, filetype="pdf")
    try:
        for pno in range(doc.page_count):
            images: list[dict] = []
            for img in doc[pno].get_images(full=True):
                xref = img[0]
                info = doc.extract_image(xref)
                ext = (info.get("ext") or "").lower()
                if ext not in _RASTER_EXTS:
                    continue
                images.append(
                    {
                        "name": f"p{pno + 1}_{xref}.{ext}",
                        "mime": _EXT_MIME.get(ext, "image/png"),
                        "data": info["image"],
                    }
                )
            if images:
                out[pno + 1] = images
    finally:
        doc.close()
    return out


def _blip_rids(paragraph) -> list[str]:
    """Return the image relationship ids anchored in a docx paragraph (a:blip r:embed)."""
    from docx.oxml.ns import qn

    rids: list[str] = []
    for blip in paragraph._p.iter(qn("a:blip")):
        rid = blip.get(qn("r:embed"))
        if rid:
            rids.append(rid)
    return rids


def _scan_docx(data: bytes) -> dict[int, list[dict]]:
    """Map paragraph index → images anchored in that paragraph.

    Headers/footers/text boxes/table cells are out of scope (they are not ``document.paragraphs``);
    the paragraph index matches ``_extract_docx(..., para_markers=True)`` exactly, since both
    iterate ``document.paragraphs`` in document order.
    """
    import docx as docx_lib

    doc = docx_lib.Document(io.BytesIO(data))
    part_info: dict[str, dict] = {}
    for rId, rel in doc.part.rels.items():
        if not (rel.reltype or "").endswith("/image"):
            continue
        part = rel.target_part
        name = str(part.partname)
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext not in _RASTER_EXTS:
            continue
        stem = name.rsplit("/", 1)[-1] or f"{rId}.{ext}"
        part_info[rId] = {
            "mime": part.content_type or _EXT_MIME.get(ext, "image/png"),
            "data": part.blob,
            "stem": stem,
        }
    out: dict[int, list[dict]] = {}
    for i, para in enumerate(doc.paragraphs):
        images: list[dict] = []
        for rid in _blip_rids(para):
            info = part_info.get(rid)
            if info is None:
                continue
            images.append(
                {"name": f"p{i}_{info['stem']}", "mime": info["mime"], "data": info["data"]}
            )
        if images:
            out[i] = images
    return out


def _scan_pptx(data: bytes) -> dict[int, list[dict]]:
    """Map 1-based slide number → raster pictures placed on that slide.

    Slides ride the same ``[[PAGE:n]]`` anchor axis as PDF (``_extract_ppt`` emits one
    ``[[PAGE:i]]`` per slide), so the chunk annotation state machine in ``tasks.py``
    needs no new axis. Group shapes are recursed; the package is first normalized via
    :func:`_ppt_as_presentation` so .potx/.ppsx scan like .pptx.
    """
    import io as _io

    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    from core.infrastructure.ingest import _ppt_as_presentation

    prs = Presentation(_io.BytesIO(_ppt_as_presentation(data)))
    out: dict[int, list[dict]] = {}

    def walk(shapes, slide_no: int, bucket: list[dict]) -> None:
        for idx, sh in enumerate(shapes):
            if sh.shape_type == MSO_SHAPE_TYPE.GROUP:
                walk(sh.shapes, slide_no, bucket)
                continue
            if sh.shape_type != MSO_SHAPE_TYPE.PICTURE:
                continue
            try:
                image = sh.image
            except Exception:
                continue
            ext = (image.ext or "").lower()
            if ext not in _RASTER_EXTS:
                continue
            bucket.append(
                {
                    "name": f"slide{slide_no}_{idx + 1}.{ext}",
                    "mime": image.content_type or _EXT_MIME.get(ext, "image/png"),
                    "data": image.blob,
                }
            )

    for sno, slide in enumerate(prs.slides, start=1):
        bucket: list[dict] = []
        walk(slide.shapes, sno, bucket)
        if bucket:
            out[sno] = bucket
    return out


_DOC_PARA_MARKER = re.compile(r"\[\[PARA:(\d+)\]\]")


def _scan_doc(data: bytes) -> dict[int, list[dict]]:
    """Map paragraph index → embedded images for a legacy .doc (OLE2) container.

    Word 97 has no rels manifest, so :func:`scan_doc_images` recovers the pixels by
    magic-header scan (file order). Anchoring reuses the same ``[[PARA:n]]`` axis as
    DOCX: antiword leaves a ``[pic]`` placeholder per inline picture, so the Nth image
    travels with the paragraph containing the Nth placeholder; when the text pass is
    unavailable (no antiword) or the counts disagree, images fall back to ordinal
    paragraph anchors — the assets still save and get captions, only the text-chunk
    co-location becomes approximate.
    """
    from core.infrastructure.doc_images import scan_doc_images

    images, skipped = scan_doc_images(data)
    if skipped:
        log.info("doc scan: %d metafile image(s) not renderable on this platform", skipped)
    if not images:
        return {}
    pic_paras: list[int] = []
    try:
        from core.infrastructure.ingest import extract_text

        text = extract_text(data, "doc.doc", para_markers=True)
        para = 0
        for line in text.split("\n"):
            m = _DOC_PARA_MARKER.match(line)
            if m:
                para = int(m.group(1))
            pic_paras.extend([para] * line.count("[pic]"))
    except Exception as exc:  # noqa: BLE001 — anchoring is best-effort, saving is not
        log.debug("doc anchor pass unavailable: %s", exc)
    out: dict[int, list[dict]] = {}
    for n, img in enumerate(images):
        anchor = pic_paras[n] if n < len(pic_paras) else n
        out.setdefault(anchor, []).append(img)
    return out


def scan_embedded_images(data: bytes, name: str) -> dict[int, list[dict]]:
    """Return anchor → images for a PDF/DOCX/PPTX/DOC, or ``{}`` for formats with no image pass."""
    ext = (name or "").rsplit(".", 1)[-1].lower()
    if ext == "pdf":
        return _scan_pdf(data)
    if ext == "docx":
        return _scan_docx(data)
    if ext == "doc":
        return _scan_doc(data)
    if ext in {"pptx", "potx", "ppsx"}:
        return _scan_pptx(data)
    return {}


async def save_images(
    scans: dict[int, list[dict]],
    doc_title: str,
    user_id,
    workspace_id,
    source_asset_id,
    drive,
) -> dict[int, list[str]]:
    """Persist scanned images as drive assets; return ``anchor → [asset_id, ...]``.

    Dedupes on ``(source_asset_id, content-hash)`` so a re-ingest of the same document
    reuses existing image assets (no duplicate rows), and so the same image anchored on
    multiple pages resolves to one asset referenced by all of them.
    """
    folder = f"RAG 图片/{_sanitize_folder(doc_title)}"
    out: dict[int, list[str]] = {}
    assets = drive.assets
    for key, images in scans.items():
        ids: list[str] = []
        for img in images:
            digest = hashlib.sha256(img["data"]).hexdigest()
            existing = await assets.get_by_source_content(source_asset_id, digest)
            if existing is not None:
                ids.append(str(existing.id))
                continue
            asset = await drive.save_artifact(
                user_id,
                img["name"],
                img["mime"],
                img["data"],
                folder_path=folder,
                workspace_id=workspace_id,
                source_asset_id=source_asset_id,
            )
            ids.append(str(asset.id))
        out[key] = ids
    return out


async def caption_chunks(
    scans: dict[int, list[dict]],
    image_ids: dict[int, list[str]],
    doc_title: str,
    *,
    llm,
    session_factory,
    axis_key: str,
    anchor_label: str,
    max_concurrency: int = 4,
    user_id=None,
) -> list:
    """Vision-LLM caption every unique scanned image → searchable leaf chunks.

    The caption turns pixels into text so the image's subject matter is recallable by the
    standard vector/keyword pipeline — the retrieval hit still carries ``image_id``, so the
    agent can open the original with the ``vision`` tool. Rules:

    - one caption per unique image bytes (same picture on several anchors is described
      once, riding all its anchors);
    - per-image failure logs a warning and skips — captioning must never fail the ingest;
      an unauthorized vision channel (``VisionNotAuthorized``) or an exhausted trial chain
      (``VisionUnsupported``) are such skips;
    - ``user_id`` (the document owner) binds the vision call to a role inside the
      permission funnel — headless jobs resolve channels as the owner, never via an
      unbound key;
    - ``axis_key`` is the meta key the text chunks use (``pages`` / ``paras``) so caption
      and text share one anchor vocabulary; ``anchor_label`` is the human wording inside
      the caption body (``page`` / ``paragraph`` / ``slide``).
    """
    import asyncio

    from core.infrastructure.ingest import Chunk
    from core.infrastructure.vision_caption import describe_image

    # digest → {img, anchors, asset_ids}: dedupe by bytes, collect every anchor.
    jobs: dict[str, dict] = {}
    for anchor, images in scans.items():
        ids = image_ids.get(anchor, [])
        for pos, img in enumerate(images):
            if pos >= len(ids):
                continue
            digest = hashlib.sha256(img["data"]).hexdigest()
            job = jobs.get(digest)
            if job is None:
                jobs[digest] = {
                    "img": img,
                    "anchors": [anchor],
                    "asset_ids": [ids[pos]],
                }
            else:
                if anchor not in job["anchors"]:
                    job["anchors"].append(anchor)
                if ids[pos] not in job["asset_ids"]:
                    job["asset_ids"].append(ids[pos])

    sem = asyncio.Semaphore(max_concurrency)

    async def one(job: dict) -> object | None:
        anchors = sorted(job["anchors"])
        where = ", ".join(f"{anchor_label} {a}" for a in anchors)
        try:
            async with sem:
                caption = (
                    await describe_image(
                        job["img"]["data"],
                        job["img"]["mime"],
                        llm=llm,
                        session_factory=session_factory,
                        user_id=user_id,
                    )
                ).strip()
        except Exception as exc:  # noqa: BLE001 — enrichment must never fail the ingest
            log.warning("image caption failed for %s (%s): %s", doc_title, where, exc)
            return None
        if not caption:
            return None
        return Chunk(
            content_en=f"Figure in {doc_title} — {where}: {caption}",
            meta={
                "kind": "image_caption",
                "image_id": job["asset_ids"][0],
                "image_ids": list(job["asset_ids"]),
                axis_key: anchors,
            },
        )

    results = await asyncio.gather(*(one(job) for job in jobs.values()))
    return [c for c in results if c is not None]
