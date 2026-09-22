"""Phase 5A allowlist semantics: recognition only, lossless abstention always.

The DIRECT_TOOLS allowlist is FLOW CONTROL over the existing tool registry, never a
second capability system. These tests pin the two sides of that contract:

* POSITIVE: a certified turn = exactly one allowlisted tool, all slots determined
  (quoted free-text or context-resolved asset id), EN and ZH;
* NEGATIVE (the lossless fallback): zero matches, two matches, a missing slot, a
  compound second demand, a deictic-only reference, a missing asset ⇒ None — the turn
  keeps its original text and full Agent authority. A recognizer that GUESSES is a
  bug; abstaining is always safe.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from core.application.chat.actions import (
    DIRECT_TOOLS,
    ActionSchemaError,
    match_direct_tool,
    validate_action,
)


def _ctx(owned=None, attach=None):
    return SimpleNamespace(
        owned_asset_id=owned, body=SimpleNamespace(attach=attach),
    )


ASSET = _ctx(attach={"asset_id": "a-1"})
OPEN = _ctx(owned="x-9")


# ── registered-tool admission: every allowlisted tool must EXIST in the tool layer ──

def test_allowlist_is_converged_v1_set():
    assert set(DIRECT_TOOLS) == {"create_folder", "add_term", "pdf_extract_text"}


def test_workflow_backed_capabilities_are_not_allowlisted():
    # doc_mindmap / doc_slides exist as tools, but their PRODUCT paths are the toolkit
    # workflow engines (mindmap_gen / slides_gen). A same-named wrapper does not make
    # the intent a single-shot atomic call — they must stay off the fast path.
    for name in ("doc_mindmap", "doc_slides", "doc_outline"):
        assert name not in DIRECT_TOOLS


@pytest.mark.parametrize("tool", ["create_folder", "add_term", "pdf_extract_text"])
def test_allowlisted_tools_are_registered_agent_tools(tool):
    # The lossless-fallback invariant lives in the apps layer: the same capability a
    # DIRECT_TOOL dispatches must be callable by the LLM through the ToolRuntime
    # (registered by an auto-discovered apps/api/tools/*_tool.py module).
    import pathlib

    registry_dir = pathlib.Path(__file__).resolve().parents[1] / "apps" / "api" / "tools"
    sources = [p.read_text(encoding="utf-8") for p in registry_dir.rglob("*.py")]
    assert any(
        f'name="{tool}"' in src for src in sources
    ), f"{tool} is allowlisted but never registered as an agent tool"


# ── positive recognitions (EN + ZH) ────────────────────────────────────────────────

POS = [
    ('新建文件夹"项目资料"', ASSET, "create_folder", {"name": "项目资料"}),
    ('创建一个叫"论文库"的文件夹', _ctx(), "create_folder", {"name": "论文库"}),
    ('create a folder named "archive"', _ctx(), "create_folder", {"name": "archive"}),
    ('make me a new folder called "notes"', _ctx(), "create_folder", {"name": "notes"}),
    ('把"serendipity"加入我的英语词汇库', _ctx(), "add_term", {"term": "serendipity", "domain": "英语"}),
    ('将 "quantum" 添加到物理单词库', _ctx(), "add_term", {"term": "quantum", "domain": "物理"}),
    ('add "entropy" to the physics glossary', _ctx(), "add_term", {"term": "entropy", "domain": "physics"}),
    ('提取这篇文档的全文', ASSET, "pdf_extract_text", {"asset_id": "a-1"}),
    ("extract the text", ASSET, "pdf_extract_text", {"asset_id": "a-1"}),
    ("给刚上传的文件转成文本", OPEN, "pdf_extract_text", {"asset_id": "x-9"}),
]


@pytest.mark.parametrize("text,ctx,tool,args", POS)
def test_certified_direct_calls(text, ctx, tool, args):
    assert match_direct_tool(text, ctx) == {"tool": tool, "args": args}


# ── lossless abstention (recognition failure = no routing, never a guess) ─────────

NEG = [
    ("", _ctx()),
    ("hello there", _ctx()),
    ("新建文件夹", _ctx()),                              # unquoted name → parameterized by LLM
    ('新建文件夹"a"，再删除文件夹"b"', _ctx()),          # compound: two demands
    ('把"word"加入词汇库', _ctx()),                      # domain missing (bare 我的 swallowed)
    ("提取全文", _ctx()),                               # no asset in context
    ("生成大纲", ASSET),                                # not allowlisted (workflow-adjacent)
    ("生成思维导图", ASSET),                             # toolkit workflow → Agent
    ("做个PPT", ASSET),                                 # deck engine → Agent
    ("提取这篇文档的全文，并翻译成英文", ASSET),          # co-demand → Agent
    ("提取全文然后搜索相关最新新闻", ASSET),              # sequencing + web → Agent
    ("给这篇文档生成脑图", ASSET),                       # excluded tool must NOT match
]


@pytest.mark.parametrize("text,ctx", NEG)
def test_abstention_is_lossless(text, ctx):
    assert match_direct_tool(text, ctx) is None


def test_attach_object_form_resolves_asset():
    attach = SimpleNamespace(asset_id="z-7")
    assert match_direct_tool("提取全文", _ctx(attach=attach)) == {
        "tool": "pdf_extract_text", "args": {"asset_id": "z-7"},
    }


# ── schema gate (executor's final pre-seam check) ─────────────────────────────────

def test_validate_action_strips_and_bounds():
    out = validate_action("add_term", {"term": " x ", "domain": " y "})
    assert out == {"tool": "add_term", "args": {"term": "x", "domain": "y"}}


@pytest.mark.parametrize("tool,args", [
    ("nope", {}),                                        # not on the allowlist
    ("create_folder", {}),                               # missing slot
    ("create_folder", {"name": "   "}),                  # blank
    ("create_folder", {"name": 3}),                      # not a str
    ("add_term", {"term": "x" * 121, "domain": "d"}),    # over length
])
def test_validate_action_rejects(tool, args):
    with pytest.raises(ActionSchemaError):
        validate_action(tool, args)
