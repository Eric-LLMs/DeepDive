"""MultimodalIngest: physical extraction into a DocumentRepresentation — zero LLM.

The slides branch feeds this to the presentation-brief workflow. Text blocks reuse
the EXACT text the shared ingest stage already produced
(:func:`apps.api.tools.toolkit.sources.load_sources`), so every
``start_line``/``end_line`` lands on the same 1-based line the shipped
``[name:line]`` citation convention points at — forged line numbers stay
structurally impossible (§9.2). Visual extraction is PDF-only and purely physical:

* raster: embedded images whose sides are all >= ``min_raster_side_px``;
* vector clusters: ``get_drawings()`` rects padded by ``cluster_tolerance_pt`` and
  merged until stable, accepted above the ``min_cluster_*_pt`` noise floor,
  rendered as 200-dpi clip PNGs;
* page fallback: pages whose drawing/image counts exceed the high-density
  thresholds get one 150-dpi full-page crop instead (clustering is unreliable there).

Slices land under ``<workspace>/.toolkit_deck_assets/<deck_id>/``; only the largest
``max_vlm_assets`` candidates are ever written (cost guard — below the cut nothing
is rendered, so nothing is paid for twice). Non-PDF inputs (Markdown, .srt, session
transcripts) get a text-only representation; transcript ``<!-- msg:ID -->`` markers
become ``message_id`` locators, preserving the shipped session traceability.
"""
from __future__ import annotations

import asyncio
import logging
import math
import re
from pathlib import Path

from . import schema as S
from .schema import PresentationWorkflowConfig

log = logging.getLogger(__name__)

# Directory (under the workspace) holding one slice folder per in-flight deck run.
ASSET_DIR = ".toolkit_deck_assets"

_MSG_MARKER_RE = re.compile(r"^<!--\s*msg:(\S+)\s*-->\s*$")
_CAPTION_RE = re.compile(
    r"(?im)^\s*((?:figure|fig\.|table|chart)\s\S[^\n]{0,160}|[图表]\s*\d[^\n]{0,80})"
)


# ── text channel (deterministic, shared by every source kind) ────────────────

def blocks_from_text(text: str, doc_id: str, *, first_id: int = 1) -> list[S.TextBlock]:
    """Split extracted text into paragraph TextBlocks with REAL line numbers.

    Lines come from ``splitlines`` over the exact string the pipeline token-counts
    and cites against, so block locators and ``[name:line]`` citations can never
    drift apart. A ``<!-- msg:ID -->`` marker line is consumed (not kept in the
    block text) and attached to the block that follows it.
    """
    blocks: list[S.TextBlock] = []
    cur: list[tuple[int, str]] = []
    pending_msg: str | None = None

    def flush() -> None:
        nonlocal cur, pending_msg
        if not cur:
            return
        blocks.append(S.TextBlock(
            block_id=f"blk_{first_id + len(blocks)}",
            text="\n".join(ln for _, ln in cur),
            locator=S.SourceLocator(
                doc_id=doc_id, start_line=cur[0][0], end_line=cur[-1][0],
                message_id=pending_msg,
            ),
        ))
        cur = []
        pending_msg = None

    for idx, line in enumerate(text.splitlines(), 1):
        marker = _MSG_MARKER_RE.match(line)
        if marker:
            flush()                     # a marker belongs to the block that follows
            pending_msg = marker.group(1)
            continue
        if line.strip():
            cur.append((idx, line))
            continue
        flush()
    flush()
    return blocks


def guess_document_title(sources_text: str, fallback: str) -> str:
    """First markdown H1 near the top of the extracted text, else ``fallback``."""
    for line in sources_text.splitlines()[:20]:
        if line.startswith("# "):
            t = line[2:].strip()
            if t:
                return t
    return fallback


# ── visual channel (PDF only; no inference, no LLM) ──────────────────────────

def _cluster_rects(rects: list, tolerance: float) -> list:
    """Pad rects, merge overlaps until stable; return unions of the ORIGINAL rects.

    Members are tracked so the emitted bbox is the true figure extent, while the
    merge decision uses the padded proxies (near-adjacency counts as one figure).
    """
    from pymupdf import Rect

    pads = [Rect(r.x0 - tolerance, r.y0 - tolerance, r.x1 + tolerance, r.y1 + tolerance)
            for r in rects]
    groups: list[list[int]] = [[i] for i in range(len(rects))]
    changed = True
    while changed:
        changed = False
        for i in range(len(groups)):
            for j in range(i + 1, len(groups)):
                if not groups[i] or not groups[j]:
                    continue
                if any(pads[a].intersects(pads[b])
                       for a in groups[i] for b in groups[j]):
                    groups[i] += groups[j]
                    groups[j] = []
                    changed = True
    out = []
    for g in groups:
        if not g:
            continue
        x0 = min(rects[k].x0 for k in g)
        y0 = min(rects[k].y0 for k in g)
        x1 = max(rects[k].x1 for k in g)
        y1 = max(rects[k].y1 for k in g)
        out.append(Rect(x0, y0, x1, y1))
    return out


def _ingest_pdf_sync(pdf_path: Path, doc_id: str, src_index: int,
                     assets_dir: Path, cfg: PresentationWorkflowConfig
                     ) -> tuple[int, str, list[S.VisualAsset]]:
    import pymupdf

    doc = pymupdf.open(str(pdf_path))
    try:
        page_count = doc.page_count
        meta_title = ((doc.metadata or {}).get("title") or "").strip()
        # Pass 1: enumerate physical candidates (cheap metadata only).
        cands: list[dict] = []
        hints: dict[int, str | None] = {}
        for pi in range(page_count):
            page = doc[pi]
            drawings = page.get_drawings() or []
            images = page.get_images(full=True) or []
            cap = _CAPTION_RE.search(page.get_text("text") or "")
            hints[pi + 1] = cap.group(1).strip()[:200] if cap else None

            if (len(drawings) > cfg.high_density_drawings
                    or len(images) > cfg.high_density_images):
                cands.append({"kind": "page", "page": pi + 1, "seq": 1,
                              "bbox": None, "area": math.inf})
                continue

            for ii, img in enumerate(images):
                xref = img[0]
                try:
                    info = doc.extract_image(xref)
                except Exception as exc:  # noqa: BLE001 - undecodable stream: skip, log
                    log.info("ingest: skipping undecodable image xref=%s: %s", xref, exc)
                    continue
                if min(info["width"], info["height"]) < cfg.min_raster_side_px:
                    continue
                rects = page.get_image_rects(xref)
                bbox = tuple(float(v) for v in rects[0]) if rects else None
                area = ((bbox[2] - bbox[0]) * (bbox[3] - bbox[1]) if bbox
                        else float(info["width"] * info["height"]))
                cands.append({"kind": "raster", "page": pi + 1, "seq": ii + 1,
                              "xref": xref, "ext": info["ext"], "bbox": bbox,
                              "area": area})

            rects = [d["rect"] for d in drawings
                     if d.get("rect") is not None and not d["rect"].is_empty]
            for vi, bbox in enumerate(_cluster_rects(rects, cfg.cluster_tolerance_pt), 1):
                w, h = bbox.width, bbox.height
                if w < cfg.min_cluster_w_pt or h < cfg.min_cluster_h_pt:
                    continue
                cands.append({"kind": "vector", "page": pi + 1, "seq": vi,
                              "bbox": (bbox.x0, bbox.y0, bbox.x1, bbox.y1),
                              "area": float(w * h)})

        # Cost guard: the VLM stage only ever sees the top max_vlm_assets by area;
        # writing more would just litter the workspace.
        cands.sort(key=lambda c: (-c["area"], c["page"], c["kind"], c["seq"]))
        kept = sorted(cands[: cfg.max_vlm_assets],
                      key=lambda c: (c["page"], c["kind"], c["seq"]))

        # Pass 2: render/extract only the kept candidates, deterministic order.
        assets: list[S.VisualAsset] = []
        assets_dir.mkdir(parents=True, exist_ok=True)
        kinds = {"raster": "r", "vector": "v", "page": "f"}
        for c in kept:
            page = doc[c["page"] - 1]
            if c["kind"] == "raster":
                info = doc.extract_image(c["xref"])
                name = f"raster_p{c['page']}_{c['seq']}.{c['ext']}"
                (assets_dir / name).write_bytes(info["image"])
                vtype = S.VisualAssetType.RASTER_IMAGE
            elif c["kind"] == "vector":
                pix = page.get_pixmap(clip=pymupdf.Rect(*c["bbox"]),
                                      matrix=pymupdf.Matrix(200 / 72, 200 / 72))
                name = f"vector_p{c['page']}_{c['seq']}.png"
                pix.save(str(assets_dir / name))
                vtype = S.VisualAssetType.VECTOR_REGION
            else:
                pix = page.get_pixmap(matrix=pymupdf.Matrix(150 / 72, 150 / 72))
                name = f"page_p{c['page']}.png"
                pix.save(str(assets_dir / name))
                vtype = S.VisualAssetType.PAGE_FALLBACK_CROP
            assets.append(S.VisualAsset(
                asset_id=f"ast{src_index}_p{c['page']}_{kinds[c['kind']]}{c['seq']}",
                page=c["page"], type=vtype,
                path=str(assets_dir / name),
                bbox=list(c["bbox"]) if c["bbox"] else None,
                semantic_hint=hints.get(c["page"]),
            ))
        return page_count, meta_title, assets
    finally:
        doc.close()


# ── the entry point ──────────────────────────────────────────────────────────

async def build_document_representation(
    sources, *, workspace: Path, deck_id: str,
    config: PresentationWorkflowConfig | None = None,
) -> S.DocumentRepresentation:
    """One multimodal :class:`DocumentRepresentation` over the pipeline's sources.

    ``sources`` are the validated :class:`WorkspaceSource` items of the run; the
    text channel reads their ``.text`` verbatim, the visual channel opens only
    ``.pdf`` files. Zero LLM, zero network — pure PyMuPDF physics.
    """
    cfg = config or PresentationWorkflowConfig()
    assets_dir = Path(workspace) / ASSET_DIR / deck_id
    blocks: list[S.TextBlock] = []
    visuals: list[S.VisualAsset] = []
    page_count = 1
    title = ""
    for si, src in enumerate(sources, 1):
        blocks.extend(blocks_from_text(src.text, src.name, first_id=len(blocks) + 1))
        path = Path(src.path)
        if path.suffix.lower() == ".pdf":
            pc, pdf_title, new_assets = await asyncio.to_thread(
                _ingest_pdf_sync, path, src.name, si, assets_dir, cfg)
            page_count = pc
            visuals.extend(new_assets)
            title = title or pdf_title
    if not title:
        title = guess_document_title(sources[0].text, Path(sources[0].name).stem)
    return S.DocumentRepresentation(
        doc_id=sources[0].name, document_title=title, page_count=page_count,
        text_blocks=blocks, visual_assets=visuals,
    )


def cleanup_stale_assets(workspace: Path, *, max_age_s: int = 24 * 3600) -> int:
    """Delete per-deck slice folders untouched for longer than ``max_age_s``.

    Mirrors :func:`session_source.cleanup_stale_sources`: a worker killed
    mid-run can leave slices behind; the next startup sweeps them. Returns the
    number of deck folders removed.
    """
    import shutil
    import time

    root = Path(workspace) / ASSET_DIR
    if not root.is_dir():
        return 0
    now = time.time()
    removed = 0
    for sub in root.iterdir():
        if not sub.is_dir():
            continue
        try:
            newest = max((f.stat().st_mtime for f in sub.rglob("*") if f.is_file()),
                         default=now)
        except OSError:
            continue
        if now - newest > max_age_s:
            try:
                shutil.rmtree(sub)
                removed += 1
            except OSError:
                continue
    return removed
