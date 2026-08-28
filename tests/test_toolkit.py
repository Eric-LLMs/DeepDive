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
        self.complete_json_calls: list[tuple[str, str]] = []

    async def complete(self, text: str, system: str) -> str:
        self.complete_calls.append((text, system))
        if "excerpt summarizer" in system:
            return "Digest fact [doc.md:1-1]"
        if "digest merger" in system:
            return "Merged digest [doc.md:1-1]"
        return ""

    async def complete_json(self, text: str, system: str) -> dict:
        self.complete_json_calls.append((text, system))
        if not self._responses:
            raise ValueError("no JSON response queued")
        return self._responses.pop(0)


def _doc(workspace: Path, name: str = "doc.md", text: str = "# Title\n\nBody text here.\n") -> Path:
    path = workspace / name
    path.write_text(text, encoding="utf-8")
    return path


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


# ── token budget / map-reduce ──

def test_token_count_heuristic_fallback(monkeypatch):
    monkeypatch.setitem(__import__("sys").modules, "tiktoken", None)  # force offline fallback
    assert sources.token_count("hello") == 1  # 5 non-CJK chars // 4
    assert sources.token_count("中文") == 3  # 2 CJK + max(1, 0)
    assert sources.token_count("") >= 1


def test_split_with_lines_preserves_ranges():
    text = "\n".join(f"line {i}" for i in range(60))
    chunks = sources._split_with_lines(text, 200)
    assert "".join(c[0] for c in chunks) == text
    for piece, start, end in chunks:
        assert end - start + 1 == piece.count("\n") + 1


async def test_budget_plan_map_reduces_when_over_budget(monkeypatch):
    monkeypatch.setattr(settings, "toolkit_max_input_tokens", 10)
    src = sources.WorkspaceSource(
        name="doc.md", path="doc.md", text="# Title\n\n" + "para\n" * 40,
        char_count=1, line_count=1,
    )
    llm = _FakeLLM()
    planned = await sources.budget_plan([src], llm)
    assert len(planned) == 1
    assert planned[0].name == "merged"
    assert "Digest fact" in planned[0].text
    assert any("excerpt summarizer" in sys for _, sys in llm.complete_calls)


async def test_budget_plan_unchanged_under_budget():
    src = sources.WorkspaceSource(name="doc.md", path="doc.md", text="short", char_count=5, line_count=1)
    planned = await sources.budget_plan([src], _FakeLLM())
    assert planned == [src]


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


async def test_slides_writes_marp_and_pptx(tmp_path):
    _doc(tmp_path, text="# Title\n\n" + "Body.\n" * 20)
    pipe = ToolKitPipeline(_FakeLLM([SLIDES_DATA]), "slides", workspace=tmp_path)
    result = await pipe.run(["doc.md"])
    assert len(result.files) == 2
    md = next(f for f in result.files if f.endswith(".md"))
    pptx = next(f for f in result.files if f.endswith(".pptx"))
    assert Path(md).is_file()
    assert Path(md).read_text(encoding="utf-8").startswith("---\nmarp: true")
    assert Path(pptx).is_file()
    assert Path(pptx).read_bytes().startswith(b"PK")  # a real zip/pptx


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
    _doc(tmp_path)
    llm = _FakeLLM([SLIDES_DATA])
    plugin = build_toolkit_plugin("slides", llm, workspace=tmp_path)
    runtime = ToolRuntime()
    runtime.register(plugin.tools[0])

    res = await runtime.execute(ToolExecution("t1", "slides_gen", {"paths": ["doc.md"]}))
    assert res.is_error is False
    rendered = str(res.value)
    assert "slides" in rendered and ".pptx" in rendered


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
