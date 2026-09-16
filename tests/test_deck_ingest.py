"""MultimodalIngest tests: physical extraction only, zero LLM (no fake needed).

Pins the contract the brief workflow relies on: paragraph TextBlocks carry the
REAL line numbers of the shared extracted text (and transcript ``message_id``
markers survive as locators); PDF slices come out of the three physical channels
with provenance (page/bbox/path); the noise floor and the ``max_vlm_assets`` cost
guard keep small images and long tails off disk; the whole build is deterministic.
The PDFs are synthesized with PyMuPDF + Pillow (both already in the lock).
"""
from __future__ import annotations

import inspect
import io
import os
import time
from pathlib import Path

import pymupdf
import pytest
from PIL import Image

from apps.api.tools.toolkit.deck import ingest as IG
from apps.api.tools.toolkit.deck import schema as S
from apps.api.tools.toolkit.sources import WorkspaceSource


def _png(w: int, h: int, color=(20, 40, 60)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "png")
    return buf.getvalue()


def _src(text: str, name: str = "doc.md", path: str | None = None) -> WorkspaceSource:
    return WorkspaceSource(
        name=name, path=str(path or name), text=text,
        char_count=len(text), line_count=text.count("\n") + 1,
    )


def _make_pdf(path: Path) -> None:
    """3 pages: p1 raster (big) + embedded noise (small) + caption; p2 a drawable
    cluster sized past the floors; p3 high drawing density → page fallback."""
    doc = pymupdf.open()
    p1 = doc.new_page(width=595, height=842)
    p1.insert_text((72, 90), "Retrieval anchors generation; cost 0.565 USD.")
    p1.insert_text((72, 110), "Figure 1: the RAG pipeline")
    p1.insert_image(pymupdf.Rect(60, 150, 360, 450), pixmap=pymupdf.Pixmap(_png(300, 300)))
    p1.insert_image(pymupdf.Rect(400, 150, 460, 210), pixmap=pymupdf.Pixmap(_png(60, 60, (9, 9, 9))))

    p2 = doc.new_page(width=595, height=842)
    sh = p2.new_shape()
    sh.draw_rect(pymupdf.Rect(60, 100, 110, 140))
    sh.draw_rect(pymupdf.Rect(130, 100, 180, 140))
    sh.finish(color=(0, 0, 0.9), fill=(0.85, 0.85, 1.0))   # path A: 60,100..180,140
    sh.draw_rect(pymupdf.Rect(60, 160, 110, 200))
    sh.finish(color=(0.6, 0, 0.9))                          # path B: 60,160..110,200
    sh.commit()                                             # A+B merge into one cluster

    p3 = doc.new_page(width=595, height=842)
    sh3 = p3.new_shape()
    for i in range(40):
        x = 60 + (i % 8) * 55
        y = 80 + (i // 8) * 40
        sh3.draw_rect(pymupdf.Rect(x, y, x + 30, y + 20))
        sh3.finish(color=(0.2, 0.2, 0.2))                  # one path per rect: 40 drawings
    sh3.commit()
    doc.save(str(path))
    doc.close()


# ── text channel ──────────────────────────────────────────────────────────────

def test_blocks_have_real_line_numbers():
    text = "# Title\n\npara one line\npara one second line\n\nsecond para\n"
    blocks = IG.blocks_from_text(text, "doc.md")
    assert [b.locator.start_line for b in blocks] == [1, 3, 6]
    assert [b.locator.end_line for b in blocks] == [1, 4, 6]
    assert blocks[1].text == "para one line\npara one second line"
    assert all(b.locator.doc_id == "doc.md" for b in blocks)


def test_msg_markers_become_message_id_locators():
    text = "<!-- msg:u1 -->\n**User:** hello\n\n<!-- msg:a2 -->\n**Assistant:** hi\n"
    blocks = IG.blocks_from_text(text, "t.md")
    assert len(blocks) == 2
    assert blocks[0].text == "**User:** hello"          # marker consumed, not kept
    assert blocks[0].locator.message_id == "u1"
    assert (blocks[0].locator.start_line, blocks[0].locator.end_line) == (2, 2)
    assert blocks[1].locator.message_id == "a2"
    assert (blocks[1].locator.start_line, blocks[1].locator.end_line) == (5, 5)


def test_block_ids_keep_counting_across_sources():
    a = IG.blocks_from_text("one\n\ntwo\n", "a.md")
    b = IG.blocks_from_text("three\n", "b.md", first_id=len(a) + 1)
    assert [x.block_id for x in a + b] == ["blk_1", "blk_2", "blk_3"]


def test_guess_document_title_prefers_h1():
    assert IG.guess_document_title("junk\n# Real Title\nbody", "fallback") == "Real Title"
    assert IG.guess_document_title("no heading here", "fallback") == "fallback"


# ── visual channel (PDF physics) ─────────────────────────────────────────────

async def test_pdf_ingest_three_channels_with_provenance(tmp_path):
    pdf = tmp_path / "paper.pdf"
    _make_pdf(pdf)
    src = _src("body line one\nline two\n", name="paper.pdf", path=pdf)
    rep = await IG.build_document_representation(
        [src], workspace=tmp_path, deck_id="d1")

    by_type = {}
    for a in rep.visual_assets:
        by_type.setdefault(a.type, []).append(a)
    # p1: the 300px image survives, the 60px one is below the noise floor
    assert [a.page for a in by_type[S.VisualAssetType.RASTER_IMAGE]] == [1]
    assert by_type[S.VisualAssetType.RASTER_IMAGE][0].bbox == pytest.approx(
        [60.0, 150.0, 360.0, 450.0])
    # p2: the merged drawing cluster (x 60..180, y 100..200) clears both floors
    vec = by_type[S.VisualAssetType.VECTOR_REGION]
    assert [a.page for a in vec] == [2]
    assert vec[0].bbox == pytest.approx([60.0, 100.0, 180.0, 200.0])
    # p3: 40 drawings > high_density_drawings → one full-page crop, no clusters
    fb = by_type[S.VisualAssetType.PAGE_FALLBACK_CROP]
    assert [a.page for a in fb] == [3] and fb[0].bbox is None

    for a in rep.visual_assets:
        f = Path(a.path)
        assert f.is_file() and f.stat().st_size > 0
        assert f.parent == tmp_path / IG.ASSET_DIR / "d1"
    assert len({a.asset_id for a in rep.visual_assets}) == len(rep.visual_assets)
    # caption scan feeds the semantic hint on the page that has one
    assert rep.visual_assets[0].semantic_hint == "Figure 1: the RAG pipeline"
    assert rep.page_count == 3
    # text blocks keep the [name:line] convention regardless of the visual channel
    assert [(b.locator.start_line, b.locator.end_line) for b in rep.text_blocks] == [(1, 2)]


async def test_non_pdf_source_is_text_only(tmp_path):
    rep = await IG.build_document_representation(
        [_src("# My Doc\n\nsome transcript content\n")], workspace=tmp_path, deck_id="d2")
    assert rep.visual_assets == []
    assert rep.document_title == "My Doc"
    assert rep.doc_id == "doc.md" and rep.page_count == 1
    assert not (tmp_path / IG.ASSET_DIR / "d2").exists()   # nothing to write, nothing made


async def test_slice_cap_is_a_cost_guard(tmp_path):
    pdf = tmp_path / "paper.pdf"
    _make_pdf(pdf)
    cfg = S.PresentationWorkflowConfig(max_vlm_assets=1)
    rep = await IG.build_document_representation(
        [_src("x\n", name="paper.pdf", path=pdf)], workspace=tmp_path, deck_id="d3",
        config=cfg)
    assert len(rep.visual_assets) == 1
    # the single kept slice is the whole-page fallback (infinite area ranks first)
    assert rep.visual_assets[0].type is S.VisualAssetType.PAGE_FALLBACK_CROP


async def test_ingest_is_deterministic(tmp_path):
    pdf = tmp_path / "paper.pdf"
    _make_pdf(pdf)
    src = _src("l1\nl2\n", name="paper.pdf", path=pdf)
    r1 = await IG.build_document_representation([src], workspace=tmp_path, deck_id="e1")
    r2 = await IG.build_document_representation([src], workspace=tmp_path, deck_id="e2")
    strip = lambda a: a.model_dump(mode="json", exclude={"path"})
    assert [strip(a) for a in r1.visual_assets] == [strip(a) for a in r2.visual_assets]
    for a, b in zip(r1.visual_assets, r2.visual_assets):
        assert Path(a.path).read_bytes() == Path(b.path).read_bytes()


def test_build_signature_carries_no_llm():
    """Zero-LLM by construction: the ingest entry point has no model parameter."""
    assert "llm" not in inspect.signature(IG.build_document_representation).parameters


# ── workspace hygiene ────────────────────────────────────────────────────────

def test_cleanup_stale_assets_removes_old_dirs_only(tmp_path):
    root = tmp_path / IG.ASSET_DIR
    fresh = root / "new"
    fresh.mkdir(parents=True)
    (fresh / "a.png").write_bytes(b"x")
    stale = root / "old"
    stale.mkdir()
    (stale / "a.png").write_bytes(b"x")
    old = time.time() - 25 * 3600
    for f in stale.rglob("*"):
        os.utime(f, (old, old))
    os.utime(stale, (old, old))

    assert IG.cleanup_stale_assets(tmp_path) == 1
    assert fresh.is_dir() and not stale.exists()


def test_cleanup_stale_assets_missing_dir_is_noop(tmp_path):
    assert IG.cleanup_stale_assets(tmp_path) == 0
