"""Phase 5 control-plane routing: ACTION / COMPOSITE certification + policy mapping,
and the C-item history invariant (fast-path requests carry NO tool traffic upstream).

Recognition (L0) stays pure: only the extractor's exact-one-hit contract can set
``requested_action``; the policy mapper maps demands to plans and never validates or
authorizes. A turn the fast path declines lands on the Agent with its ORIGINAL text —
the byte-clean hand-off is pinned end-to-end in test_chat_action_e2e.
"""
from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from core.application.chat.execution_plan import (
    PlanKind,
    PolicyContext,
    build_execution_plan,
)
from core.application.chat.executors.base import TurnRequest
from core.application.chat.executors.direct import DirectExecutor
from core.application.chat.understanding import (
    Complexity,
    Confidence,
    Signal,
    resolve_requirements,
)

ALL_ON = PolicyContext(
    fast_paths_enabled=True, direct_fast_path_enabled=True, viewer_fast_path_enabled=True,
    retrieval_fast_path_enabled=True, action_enabled=True, composite_enabled=True,
)


def _block():
    return SimpleNamespace(kind="selection", image_asset_id=None)


def _ctx(message="hi", *, attach=None, owned=None, viewer=None):
    return SimpleNamespace(
        body=SimpleNamespace(message=message, attach=attach),
        user_id=uuid4(), user_text=message, owned_asset_id=owned,
        viewer_assembly=viewer, research_turn=False, effective_handoff=None,
    )


def _facts(message, **kw):
    return resolve_requirements(_ctx(message, **kw), message)


# ── ACTION certification ───────────────────────────────────────────────────────────

def test_certified_action_turn_routes_action():
    req = _facts('新建文件夹"项目资料"')
    assert req.needs_action is Signal.HIGH and req.confidence is Confidence.HIGH
    assert req.requested_action == {"tool": "create_folder", "args": {"name": "项目资料"}}
    plan = build_execution_plan(req, ALL_ON)
    assert plan.kind is PlanKind.ACTION and plan.action == req.requested_action
    assert plan.reason.startswith("phase5a")


def test_asset_tool_action_certifies_with_attach_context():
    req = _facts("提取这篇文档的全文", attach={"asset_id": "a-1"})
    assert req.requested_action == {"tool": "pdf_extract_text", "args": {"asset_id": "a-1"}}
    assert build_execution_plan(req, ALL_ON).kind is PlanKind.ACTION


def test_action_gate_off_maps_to_agent():
    req = _facts('create a folder named "x"')
    dark = PolicyContext(
        fast_paths_enabled=True, action_enabled=False,
    )
    assert build_execution_plan(req, dark).kind is PlanKind.AGENT


def test_master_gate_off_keeps_action_turn_dark():
    req = _facts('新建文件夹"x"')
    dark = PolicyContext(fast_paths_enabled=False, action_enabled=True)
    assert build_execution_plan(req, dark).kind is PlanKind.AGENT


@pytest.mark.parametrize("msg", [
    '新建文件夹"a"，然后搜索最新新闻',       # compound + web → extractor abstains
    "create a folder for my notes",             # action word, but unquoted name → no cert
    "提取全文",                                 # no asset in context → no cert
])
def test_uncertain_action_turns_never_certify(msg):
    req = _facts(msg)
    assert req.requested_action is None
    plan = build_execution_plan(req, ALL_ON)
    assert plan.kind is not PlanKind.ACTION     # never a partial/guess dispatch;
    # and whenever any capability demand remains, the Agent keeps full authority.
    if req.needs_action is Signal.HIGH or req.needs_web is Signal.HIGH:
        assert plan.kind is PlanKind.AGENT


# ── COMPOSITE certification (viewer text + private recall, independent only) ───────

def _viewer():
    return {"status": "injected", "blocks": [_block()]}


COMPOSITE_MSG = "这一页讲的内容，我的知识库里还有哪些相关资料"


def test_viewer_plus_private_certifies_composite():
    req = _facts(COMPOSITE_MSG, viewer=_viewer())
    assert req.needs_viewer is Signal.HIGH and req.needs_private is Signal.HIGH
    assert req.confidence is Confidence.HIGH and req.complexity is Complexity.MODERATE
    plan = build_execution_plan(req, ALL_ON)
    assert plan.kind is PlanKind.COMPOSITE
    assert plan.subrequests == ("viewer_text", "private_recall")
    assert plan.requires_viewer and plan.requires_retrieval


def test_composite_inherits_private_source_policy():
    """Constraint 2 at the policy layer: COMPOSITE fences like LOCAL_RAG, so the
    orchestrator's pre-dispatch sink gives an escalation the same web ban."""
    assert build_execution_plan(_facts(COMPOSITE_MSG, viewer=_viewer()), ALL_ON).source_policy == "private_first"
    only = _facts("只用我的知识库回答，这一页提到的概念还有哪些资料", viewer=_viewer())
    assert build_execution_plan(only, ALL_ON).source_policy == "private_only"
    ext = _facts(COMPOSITE_MSG + "，没有的话可以联网搜", viewer=_viewer())
    assert build_execution_plan(ext, ALL_ON).source_policy is None


def test_composite_gate_off_maps_to_agent():
    req = _facts(COMPOSITE_MSG, viewer=_viewer())
    no_comp = PolicyContext(
        fast_paths_enabled=True, viewer_fast_path_enabled=True,
        retrieval_fast_path_enabled=True, composite_enabled=False,
    )
    assert build_execution_plan(req, no_comp).kind is PlanKind.AGENT


@pytest.mark.parametrize("msg", [
    "先总结这一页，然后再查我的知识库",
    "这一页的内容，我的知识库里有没有，然后再基于它搜索最新的进展",
])
def test_sequenced_turns_never_certify_composite(msg):
    """A→B dependency = multi-hop = the Agent's job; the L0 engine must abstain."""
    req = _facts(msg, viewer=_viewer())
    assert req.confidence is not Confidence.HIGH
    assert build_execution_plan(req, ALL_ON).kind is PlanKind.AGENT


def test_viewer_only_turn_stays_viewer_and_private_only_stays_local_rag():
    v = _viewer()
    plan = build_execution_plan(_facts("what does the selection say?", viewer=v), ALL_ON)
    assert plan.kind is PlanKind.VIEWER           # Phase 3 branch untouched
    plan = build_execution_plan(
        _facts("what does my knowledge base say about gd"), ALL_ON,
    )
    assert plan.kind is PlanKind.LOCAL_RAG        # Phase 4 branch untouched


# ── C-item: history entering a fast path is tool-free (regression pin) ─────────────

def test_fast_path_request_history_contains_no_tool_traffic():
    ctx = SimpleNamespace(
        history=[
            {"role": "user", "content": "hi", "tool_calls": [{"id": "t1"}]},
            {"role": "tool", "content": '{"secret": "tool payload"}', "tool_call_id": "t1"},
            {"role": "assistant", "content": "hello"},
            {"role": "user", "content": "again"},
            {"role": "system", "content": "not history"},
        ],
        user_text="q", model="m", base_url=None, api_key=None, disable_thinking=False,
        session_memory=None,
    )
    req = TurnRequest(
        ctx=ctx, deps=SimpleNamespace(agent=SimpleNamespace(loop=SimpleNamespace(llm=None))),
        plan=None,
    )
    sent = DirectExecutor()._build_request(req)
    # system + filtered history + this turn's user message; tool rows and the system
    # history row are dropped, and no per-message tool fields ride along.
    assert [m["role"] for m in sent] == ["system", "user", "assistant", "user", "user"]
    assert all("tool_calls" not in m and "tool_call_id" not in m for m in sent)
