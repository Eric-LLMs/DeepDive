"""Tests for the toolkit pipeline (summary / mindmap / slides).

Covers the shared 5-stage lifecycle with a hand-rolled :class:`_FakeLLM` (JSON mode +
``complete`` fallback), isolated to a ``tmp_path`` workspace so nothing touches the real
settings/workspace or a model service. Includes the Cordis-style hook interception, the
plugin tool wired through :class:`ToolRuntime.execute`, and the HTTP endpoint's path
confinement.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from agent import Context, SkillRegistry
from agent.engine.decisions import ToolExecution
from agent.engine.runtime import ToolRuntime
from agent.plugins.manager import PluginManager
from core.config import settings
from fastapi import HTTPException

from apps.api.tools.toolkit import outputs, sources
from apps.api.tools.toolkit.errors import GenerationError, SourceError
from apps.api.tools.toolkit.pipeline import ToolKitPipeline
from apps.api.tools.toolkit.plugins import build_toolkit_plugin
from apps.api.tools.toolkit.prompts import SYSTEM_PROMPTS

SUMMARY_DATA = {
    "title": "Deep Dive",
    "executive_summary": "The document explains retrieval.",
    "key_points": [{"point": "RRF blends scores.", "citations": ["[doc.md:1]"]}],
    "sections": [{"heading": "Method", "summary": "Hybrid retrieval.", "citations": ["[doc.md:2]"]}],
    "qa": [{"question": "How?", "answer": "RRF.", "citations": ["[doc.md:3]"]}],
}

MINDMAP_DATA = {
    "topic": "Retrieval",
    "branches": [
        {
            "label": "Scoring",
            "citations": ["[doc.md:1]"],
            "children": [{"label": "RRF", "children": []}],
        }
    ],
}

SLIDES_DATA = {
    "title": "Deck",
    "slides": [
        {
            "heading": "Intro",
            "core_idea": "Why retrieval.",
            "support_points": ["A", "B", "C"],
            "speaker_notes": "Open strong.",
            "citations": ["[doc.md:1]"],
        }
    ],
}


class _FakeLLM:
    """``complete_json`` first (JSON mode); ``complete`` fallback for map/reduce digests."""

    def __init__(self, responses=None) -> None:
        self._responses = list(responses or [])
        self.complete_calls: list[tuple[str, str]] = []
        self.complete_timeouts: list[float | None] = []
        self.complete_json_calls: list[tuple[str, str]] = []

    async def complete(self, text: str, system: str, timeout: float | None = None) -> str:
        self.complete_calls.append((text, system))
        self.complete_timeouts.append(timeout)
        if "excerpt summarizer" in system:
            return "Digest fact [doc.md:1-1]"
        if "digest merger" in system:
            return "Merged digest [doc.md:1-1]"
        return ""

    async def complete_json(self, text: str, system: str, timeout: float | None = None) -> dict:
        self.complete_json_calls.append((text, system))
        if not self._responses:
            raise ValueError("no JSON response queued")
        return self._responses.pop(0)


def _doc(workspace: Path, name: str = "doc.md", text: str = "# Title\n\nBody text here.\n") -> Path:
    path = workspace / name
    path.write_text(text, encoding="utf-8")
    return path


def _brief_reply(prompt: str) -> dict:
    """Scripted Pass D reply: echo the run's dynamic deck_id from the task header."""
    import re

    from tests.test_deck_workflow import brief_payload
    deck_id = re.search(r"DECK_ID: (\S+)", prompt).group(1)
    payload = brief_payload(deck_id, ["sec_1"], with_fig=False)
    payload["slides"] += [
        {**payload["slides"][0], "slide_index": 2},
        {**payload["slides"][0], "slide_index": 3},
    ]
    return payload


# ── output parsing / rendering ──

@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"a": 1}', {"a": 1}),
        ('```json\n{"a": 1}\n```', {"a": 1}),
        ("prefix {\"a\": 1} suffix", {"a": 1}),
        ("not json", None),
        ("[1, 2]", None),  # not an object
        ('{"a": 1', None),  # unparseable
    ],
)
def test_extract_json(raw, expected):
    assert outputs.extract_json(raw) == expected


def test_render_summary_md():
    md = outputs.render_summary_md(SUMMARY_DATA)
    assert md.startswith("# Deep Dive")
    assert "## Executive Summary" in md
    assert "- RRF blends scores. [doc.md:1]" in md
    assert "**Q:** How?" in md


def test_mm_label_escapes_reserved():
    assert outputs._mm_label("Node (1)") == '"Node (1)"'
    assert outputs._mm_label('say "hi"') == '"say \'hi\'"'
    assert outputs._mm_label("plain") == "plain"
    assert outputs._mm_label("") == "-"  # empty label collapses to the "-" placeholder


def test_render_mindmap_mmd_escapes_and_depths():
    data = {
        "topic": "Retrieval",
        "branches": [{"label": "Scoring (hybrid)", "children": [{"label": "RRF", "children": []}]}],
    }
    mmd = outputs.render_mindmap_mmd(data)
    assert mmd.startswith("mindmap\n")
    assert 'root((Retrieval))' in mmd
    assert '"Scoring (hybrid)"' in mmd
    # root=0, branch=1, leaf=2 — the label indentation reflects the level.
    lines = [l for l in mmd.splitlines() if l.strip()]
    assert lines[1].startswith("  root")  # branch level 0
    assert lines[2].startswith("    ")   # branch level 1
    assert lines[3].startswith("      ")  # branch level 2


def test_render_slides_marp():
    md = outputs.render_slides_marp(SLIDES_DATA)
    assert md.startswith("---\nmarp: true")
    assert "## Intro" in md
    assert "**Core idea:** Why retrieval." in md
    assert "- A" in md and "- C" in md
    assert "*Sources: [doc.md:1]*" in md
    assert "<!-- Speaker notes: Open strong. -->" in md


def test_slides_for_pptx():
    assert outputs.slides_for_pptx(SLIDES_DATA) == [("Intro", "A\nB\nC")]


# ── one-shot capacity check + explicit big-document multi-call flow ──

def test_token_count_heuristic_fallback(monkeypatch):
    monkeypatch.setitem(__import__("sys").modules, "tiktoken", None)  # force offline fallback
    assert sources.token_count("hello") == 1  # 5 non-CJK chars // 4
    assert sources.token_count("中文") == 3  # 2 CJK + max(1, 0)
    assert sources.token_count("") >= 1


def _src(text: str, name: str = "doc.md", offset: int = 1) -> sources.WorkspaceSource:
    return sources.WorkspaceSource(
        name=name, path=name, text=text, char_count=len(text),
        line_count=text.count("\n") + 1, line_offset=offset,
    )


def test_plan_big_document_none_within_capacity(monkeypatch):
    # Directive 2026-09-15: at or below capacity the COMPLETE raw text goes in ONE call.
    monkeypatch.setattr(sources.settings, "toolkit_max_input_tokens", 1000)
    assert sources.plan_big_document([_src("short raw text\n")]) is None


async def test_load_sources_over_capacity_returns_full_raw_text(tmp_path, monkeypatch):
    # load_sources NEVER fails or compresses on size — planning is the pipeline's job.
    monkeypatch.setattr(sources.settings, "toolkit_max_input_tokens", 10)
    _doc(tmp_path, text="# T\n\n" + "raw body line\n" * 30)
    loaded = await sources.load_sources(tmp_path, [tmp_path / "doc.md"], _FakeLLM())
    assert len(loaded) == 1
    assert loaded[0].text.count("raw body line") == 30  # full raw text, nothing dropped


def test_plan_big_document_splits_raw_preserving_text_and_offsets(monkeypatch):
    monkeypatch.setattr(sources, "token_count", lambda t: max(1, len(t) // 4))
    monkeypatch.setattr(sources.settings, "toolkit_max_input_tokens", 40)
    text = "\n".join(f"L{i:02d} " + "x" * 20 for i in range(8))
    batches = sources.plan_big_document([_src(text + "\n")])
    assert batches and len(batches) == 2
    assert "".join(b[0].text for b in batches) == text + "\n"  # raw, exact, complete
    assert batches[0][0].line_offset == 1
    assert batches[1][0].line_offset == 1 + sum(
        1 for _ in batches[0][0].text.rstrip("\n").split("\n"))
    for b in batches:
        assert sources.total_input_tokens(b) <= 40
    assert all(s.name == "doc.md" for b in batches for s in b)  # original names kept


def test_plan_big_document_hard_splits_single_oversized_line(monkeypatch):
    # PDF-extracted text can be ONE line with no newlines — line-granular splitting must
    # degrade to a char-level hard split, and every sub-chunk keeps the line's offset.
    monkeypatch.setattr(sources, "token_count", lambda t: max(1, len(t) // 4))
    monkeypatch.setattr(sources.settings, "toolkit_max_input_tokens", 40)
    line = "z" * 400  # 100 tokens on one line
    batches = sources.plan_big_document([_src(line)])
    assert batches and len(batches) >= 3
    assert "".join(s.text for b in batches for s in b) == line  # raw, complete
    for b in batches:
        assert b[0].line_offset == 1  # all of one original line
        assert sources.total_input_tokens(b) <= 40


def test_plan_big_document_batches_guardrail(monkeypatch):
    monkeypatch.setattr(sources, "token_count", lambda t: max(1, len(t) // 4))
    monkeypatch.setattr(sources.settings, "toolkit_max_input_tokens", 8)
    text = "".join("y" * 24 + "\n" for _ in range(200))  # 600 tokens → 75 > 64 batches
    with pytest.raises(SourceError, match="big-document calls"):
        sources.plan_big_document([_src(text)])


def test_remap_citations_shifts_batch_relative_lines():
    data = {
        "key_points": [{"point": "p", "citations": ["[doc.md:3]", "[doc.md:1-2]"]}],
        "note": "see [doc.md:5] and [file.md:2]",
    }
    out = sources.remap_citations(data, {"doc.md": 21})
    assert out["key_points"][0]["citations"] == ["[doc.md:23]", "[doc.md:21-22]"]
    assert "[doc.md:25]" in out["note"]      # offset applied
    assert "[file.md:2]" in out["note"]      # unlisted names untouched


def test_merge_summary_and_mindmap_join_structurally():
    a = {**SUMMARY_DATA, "title": "T", "executive_summary": "part one"}
    b = {**SUMMARY_DATA, "title": "", "executive_summary": "part two"}
    merged = outputs.merge_summary([a, b])
    assert merged["title"] == "T"
    assert merged["executive_summary"] == "part one\n\npart two"
    assert len(merged["key_points"]) == 2 and len(merged["sections"]) == 2
    mm_a = {**MINDMAP_DATA, "topic": "Big Topic"}
    mm = outputs.merge_mindmap([mm_a, MINDMAP_DATA])
    assert mm["topic"] == "Big Topic" and len(mm["branches"]) == 2


# ── pipeline E2E over capacity: explicit flow, raw grounding, merged result ──

async def test_summary_over_capacity_uses_raw_multi_calls(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "token_count", lambda t: max(1, len(t) // 4))
    monkeypatch.setattr(sources.settings, "toolkit_max_input_tokens", 40)
    text = "\n".join(f"L{i:02d} " + "x" * 20 for i in range(8)) + "\n"
    _doc(tmp_path, text=text)
    llm = _FakeLLM([SUMMARY_DATA, SUMMARY_DATA])
    pipe = ToolKitPipeline(llm, "summary", workspace=tmp_path)
    result = await pipe.run(["doc.md"])
    # 2 batches → 2 grounding calls, EACH on raw text, and no digest-input call anywhere.
    prompts = [p for p, _ in llm.complete_json_calls]
    assert len(prompts) == 2
    seen = "".join(prompts)
    assert all(f"L{i:02d}" in seen for i in range(8))       # full raw coverage
    assert prompts[0] != prompts[1]                          # per-batch windows
    assert all("Big-document batch" in p for p in prompts)   # announced, explicit
    out = Path(result.files[0]).read_text(encoding="utf-8")
    assert out.count("- RRF blends scores.") == 2            # structurally merged
    # batch-2 lines shifted by its offset (6 raw lines precede it → +6): [doc.md:1]→[doc.md:7]
    assert "[doc.md:7]" in out and "[doc.md:8]" in out
    assert "big-document" in result.summary                  # visible in the job record


async def test_mindmap_over_capacity_joins_branches(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "token_count", lambda t: max(1, len(t) // 4))
    monkeypatch.setattr(sources.settings, "toolkit_max_input_tokens", 40)
    text = "\n".join(f"L{i:02d} " + "x" * 20 for i in range(8)) + "\n"
    _doc(tmp_path, text=text)
    llm = _FakeLLM([MINDMAP_DATA, MINDMAP_DATA])
    pipe = ToolKitPipeline(llm, "mindmap", workspace=tmp_path)
    result = await pipe.run(["doc.md"])
    assert len(llm.complete_json_calls) == 2
    mmd = Path(result.files[0]).read_text(encoding="utf-8")
    assert mmd.count("RRF") == 2  # both batches' branches kept


async def test_slides_under_capacity_still_single_raw_call(tmp_path, monkeypatch):
    # capacity check must NOT touch the one-shot path: a small doc stays ONE Pass A call.
    monkeypatch.setattr(sources.settings, "toolkit_max_input_tokens", 1_000_000)
    assert sources.plan_big_document([_src("# T\n\nsmall body\n")]) is None


# ── pipeline stages ──

async def test_path_traversal_rejected(tmp_path):
    _doc(tmp_path)
    pipe = ToolKitPipeline(_FakeLLM(), "summary", workspace=tmp_path)
    with pytest.raises(SourceError, match="escapes workspace"):
        await pipe.run(["../escape.md"])


async def test_unsupported_format_rejected(tmp_path):
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01\x02")
    pipe = ToolKitPipeline(_FakeLLM(), "summary", workspace=tmp_path)
    with pytest.raises(SourceError, match="unsupported format"):
        await pipe.run(["blob.bin"])


async def test_no_extractable_text_rejected(tmp_path):
    (tmp_path / "notes.md").write_text("\n\n\n", encoding="utf-8")  # whitespace only
    pipe = ToolKitPipeline(_FakeLLM(), "summary", workspace=tmp_path)
    with pytest.raises(SourceError, match="no extractable text"):
        await pipe.run(["notes.md"])


async def test_schema_failure_retries_with_error_feedback(tmp_path):
    _doc(tmp_path)
    llm = _FakeLLM(responses=[{"title": "x"}, SUMMARY_DATA])  # first invalid, retry valid
    pipe = ToolKitPipeline(llm, "summary", workspace=tmp_path)
    result = await pipe.run(["doc.md"])
    assert result.tool == "summary"
    assert len(llm.complete_json_calls) == 2
    retry_prompt = llm.complete_json_calls[1][0]
    assert "conform to the schema" in retry_prompt
    assert "key_points" in retry_prompt  # the concrete error was carried back


async def test_custom_prompt_appended_to_default_system(tmp_path):
    _doc(tmp_path)
    llm = _FakeLLM([SUMMARY_DATA])
    pipe = ToolKitPipeline(llm, "summary", workspace=tmp_path)
    await pipe.run(["doc.md"], prompt="Make it bilingual (en/zh).")
    system = llm.complete_json_calls[0][1]
    assert system.startswith(SYSTEM_PROMPTS["summary"])
    assert system.endswith("Make it bilingual (en/zh).")


async def test_blank_custom_prompt_uses_default_system(tmp_path):
    _doc(tmp_path)
    llm = _FakeLLM([SUMMARY_DATA])
    pipe = ToolKitPipeline(llm, "summary", workspace=tmp_path)
    await pipe.run(["doc.md"], prompt="   ")
    assert llm.complete_json_calls[0][1] == SYSTEM_PROMPTS["summary"]


async def test_persistent_schema_failure_raises(tmp_path):
    _doc(tmp_path)
    bad = {"title": "x"}  # missing every required key
    llm = _FakeLLM(responses=[bad, bad])
    pipe = ToolKitPipeline(llm, "summary", workspace=tmp_path)
    with pytest.raises(GenerationError, match="key_points"):
        await pipe.run(["doc.md"])


async def test_mindmap_depth_cap_enforced(tmp_path):
    _doc(tmp_path)
    deep = {"topic": "T", "branches": [{"label": "a", "children": [
        {"label": "b", "children": [{"label": "c", "children": [
            {"label": "d", "children": [{"label": "e", "children": []}]}]}]}]}]}
    llm = _FakeLLM(responses=[deep, deep])
    pipe = ToolKitPipeline(llm, "mindmap", workspace=tmp_path)
    with pytest.raises(GenerationError, match="depth"):
        await pipe.run(["doc.md"])


async def test_summary_writes_md(tmp_path):
    _doc(tmp_path)
    pipe = ToolKitPipeline(_FakeLLM([SUMMARY_DATA]), "summary", workspace=tmp_path)
    result = await pipe.run(["doc.md"])
    assert result.tool == "summary"
    assert len(result.files) == 1 and result.files[0].endswith(".md")
    out = Path(result.files[0])
    assert out.is_relative_to(tmp_path)
    assert "# Deep Dive" in out.read_text(encoding="utf-8")


async def test_slides_brief_engine_writes_all_artifacts(tmp_path):
    # slides now runs the grounded visual presentation engine: multimodal ingest (text
    # channel here — a Markdown source) → A/C/D brief workflow → PresentationBrief →
    # canonical deck.pdf (via the Visual Compiler) + deck.json (the brief) + compat exports
    import json as _json
    import shutil as _shutil

    from tests.test_deck_workflow import FakeLLM, global_payload, section_payload

    if _shutil.which("typst") is None:
        import pytest
        pytest.skip("typst binary required for deck.pdf render")
    _doc(tmp_path, text="# Title\n\n" + "Retrieval anchors generation. " * 6)
    llm = FakeLLM([section_payload(), global_payload(["sec_1"]), _brief_reply])
    pipe = ToolKitPipeline(llm, "slides", workspace=tmp_path)
    result = await pipe.run(["doc.md"], count=3)  # the scripted brief carries 3 slides
    exts = {Path(f).suffix for f in result.files}
    assert exts == {".pdf", ".json", ".md", ".pptx"}
    pdf = next(Path(f) for f in result.files if f.endswith(".pdf"))
    assert pdf.read_bytes()[:5] == b"%PDF-"
    # 1 cover + 3 content slides (pages_expected formula, errata #5)
    assert _pdf_page_count(pdf) == 4
    md = next(Path(f) for f in result.files if f.endswith(".md"))
    assert md.read_text(encoding="utf-8").startswith("---\nmarp: true")
    # M2: deck.pptx is a real OOXML package (cover + one slide per plan)
    pptx = next(Path(f) for f in result.files if f.endswith(".pptx"))
    assert pptx.read_bytes()[:2] == b"PK"
    from io import BytesIO

    from pptx import Presentation
    prs = Presentation(BytesIO(pptx.read_bytes()))
    assert len(list(prs.slides)) == 4
    brief = _json.loads(
        next(Path(f) for f in result.files if f.endswith(".json")).read_text(encoding="utf-8"))
    assert len(brief["slides"]) == 3
    assert brief["traceability_graph"], "deck.json is now the canonical brief"
    # per-stage stats ride the same mechanism as before
    assert {"A/text_1", "C/reduce", "D/synthesize"} <= set(result.stats)


def _pdf_page_count(pdf: Path) -> int:
    import pymupdf
    doc = pymupdf.open(pdf)
    try:
        return doc.page_count
    finally:
        doc.close()


async def test_mindmap_writes_mmd(tmp_path):
    _doc(tmp_path)
    pipe = ToolKitPipeline(_FakeLLM([MINDMAP_DATA]), "mindmap", workspace=tmp_path)
    result = await pipe.run(["doc.md"])
    assert len(result.files) == 1 and result.files[0].endswith(".mmd")
    assert Path(result.files[0]).read_text(encoding="utf-8").startswith("mindmap\n")


async def test_before_validate_hook_can_abort(tmp_path):
    _doc(tmp_path)
    from agent.engine.events import EventBus

    events = EventBus()
    called = False

    async def block(paths, next_):
        nonlocal called
        called = True
        raise SourceError("blocked by hook")

    events.on("toolkit/before-validate", block)
    pipe = ToolKitPipeline(_FakeLLM([SUMMARY_DATA]), "summary", workspace=tmp_path, events=events)
    with pytest.raises(SourceError, match="blocked by hook"):
        await pipe.run(["doc.md"])
    assert called


async def test_after_persist_observer_runs(tmp_path):
    _doc(tmp_path)
    from agent.engine.events import EventBus

    events = EventBus()
    seen = []

    async def observer(payload):
        seen.append(payload.files)

    events.observe("toolkit/after-persist", observer)
    pipe = ToolKitPipeline(_FakeLLM([SUMMARY_DATA]), "summary", workspace=tmp_path, events=events)
    await pipe.run(["doc.md"])
    assert len(seen) == 1 and seen[0]


# ── plugin wiring ──

async def test_plugin_tool_executes_via_runtime(tmp_path):
    # generic plugin→runtime wiring, tool-agnostic: use summary (the brief engine for
    # slides is covered end-to-end by test_slides_brief_engine_writes_all_artifacts)
    _doc(tmp_path)
    llm = _FakeLLM([SUMMARY_DATA])
    plugin = build_toolkit_plugin("summary", llm, workspace=tmp_path)
    runtime = ToolRuntime()
    runtime.register(plugin.tools[0])

    res = await runtime.execute(ToolExecution("t1", "summary_gen", {"paths": ["doc.md"]}))
    assert res.is_error is False
    rendered = str(res.value)
    assert "summary" in rendered and ".md" in rendered


async def test_plugin_manager_mounts_toolkit_plugins(tmp_path):
    _doc(tmp_path)
    manager = PluginManager(ToolRuntime(), SkillRegistry(), Context())
    for tool, data in (("summary", SUMMARY_DATA), ("mindmap", MINDMAP_DATA), ("slides", SLIDES_DATA)):
        manager.register(build_toolkit_plugin(tool, _FakeLLM([data]), workspace=tmp_path))
    manager.validate()
    assert set(manager.names()) == {"toolkit_summary", "toolkit_mindmap", "toolkit_slides"}
    assert manager.runtime.get("summary_gen") is not None
    assert manager.runtime.get("mindmap_gen") is not None
    assert manager.runtime.get("slides_gen") is not None


# ── worker file-branch E2E (real pipeline + deck engine + drive save) ──

async def test_worker_files_branch_e2e_srt_deck_to_drive(monkeypatch, tmp_path):
    # In-process E2E of the worker's cloud-file drive mode: a .srt subtitle is downloaded,
    # run through the real ToolKitPipeline + brief workflow (scripted LLM, real Typst
    # compile), and every artifact named by ``artifact_plan`` is saved back into the Drive.
    import shutil as _shutil
    import uuid as _uuid
    from types import SimpleNamespace

    from apps.api.tools.toolkit.pipeline import ToolKitPipeline
    from apps.worker import tasks as worker_tasks
    from tests.test_deck_workflow import FakeLLM, global_payload, section_payload

    if _shutil.which("typst") is None:
        pytest.skip("typst binary required for deck.pdf render")

    src_text = ("RAW-SOURCE-MARKER-9f3c RAG 结合检索与生成,向量库按谓词隔离,"
                "成本 0.565 USD。")
    srt = (
        "1\n00:00:01,000 --> 00:00:05,000\n" + src_text + "\n\n"
        "2\n00:00:06,000 --> 00:00:10,000\n"
        "Vector stores isolate data with app-level predicates.\n"
    )
    owner, fid = _uuid.uuid4(), _uuid.uuid4()

    class _Drive:
        def __init__(self):
            self.saved: list[tuple[str, str, bytes]] = []

        async def download(self, user_id, file_id):
            assert user_id == owner and file_id == fid
            return "application/x-subrip", "lecture.srt", srt.encode("utf-8")

        async def save_artifact(self, user_id, name, mime, content, *, folder_path=None, workspace_id=None):
            assert user_id == owner and folder_path == "gen"
            self.saved.append((name, mime, content))
            return SimpleNamespace(id=_uuid.uuid4(), name=name, folder_path=folder_path)

    class _JobStore:
        async def get(self, job_id):
            return SimpleNamespace(user_id=owner)

    llm = FakeLLM([section_payload(), global_payload(["sec_1"]), _brief_reply])
    monkeypatch.setattr("apps.api.tools.toolkit.pipeline_for",
                        lambda tool, _llm: ToolKitPipeline(llm, tool, workspace=tmp_path))
    monkeypatch.setattr(settings, "workspace_dir", tmp_path)
    drive = _Drive()
    monkeypatch.setattr("apps.worker.tasks.DriveService", lambda _sf: drive)

    result = await worker_tasks._generate_from_files(
        {"job_store": _JobStore(), "llm": llm}, str(_uuid.uuid4()),
        {"tool": "slides", "file_ids": [str(fid)], "name": "Lecture",
         "folder_path": "gen", "count": 3},
    )

    assert result["tool"] == "slides"
    assert {a["name"] for a in result["assets"]} == {
        "Lecture_slides.pdf", "Lecture_slides.md", "Lecture_slides.pptx", "Lecture_slides.json"}
    mimes = {name: mime for name, mime, _ in drive.saved}
    assert mimes["Lecture_slides.pdf"] == "application/pdf"
    assert mimes["Lecture_slides.json"] == "application/json"
    import json as _json

    brief = _json.loads(next(c for name, _, c in drive.saved if name.endswith(".json")))
    assert brief["deck_id"] and len(brief["slides"]) == 3   # deck.json IS the brief
    pdf_bytes = next(c for name, _, c in drive.saved if name.endswith(".pdf"))
    assert pdf_bytes[:5] == b"%PDF-"
    import pymupdf

    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    try:
        assert doc.page_count == 4  # 1 cover + 3 scripted content slides
    finally:
        doc.close()
    # The downloaded temp subtitle is cleaned up after the run.
    from apps.api.tools.toolkit.session_source import SESSION_SRC_DIR

    src_dir = tmp_path / SESSION_SRC_DIR
    assert not list(src_dir.glob("lecture*.srt"))


# ── HTTP endpoint path confinement ──

def test_confined_path_rejects_escape(monkeypatch, tmp_path):
    from apps.api.routers.jobs import _confined_path

    monkeypatch.setattr(settings, "workspace_dir", tmp_path)
    # Absolute path inside the workspace passes and is normalized.
    assert _confined_path(str(tmp_path / "notes.md"), field="paths") == str(
        (tmp_path / "notes.md").resolve()
    )
    # Anything resolving outside the workspace is rejected with 400.
    with pytest.raises(HTTPException) as exc:
        _confined_path(str(tmp_path.parent / "outside.md"), field="paths")
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException):
        _confined_path(str(tmp_path.parent / "output"), field="output_dir")
