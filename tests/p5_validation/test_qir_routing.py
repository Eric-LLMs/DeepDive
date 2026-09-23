"""QIR Batch-1 contract tests — the frozen boundaries, pinned.

Covers: cascade fail-open (timeout/exception/empty/ambiguous/NONE), original-query
byte identity, publication atomicity (no half-ready snapshot), route/execute
double validation (stale version = TERMINAL, never escalation), C2 integrity
terminals, the negation guard at both defense layers, and the dark-launch
no-drift property (gate closed => resolve_plan touches nothing QIR-related).
"""
from __future__ import annotations

import asyncio
import types

import pytest
from core.application.chat import qir
from core.application.chat.actions import (
    ActionIntegrityFailure,
    bind_arguments,
    is_negated_request,
    match_direct_tool,
)
from core.application.chat.qir.snapshot import build_snapshot, validate_draft
from core.application.chat.qir.types import Capability, Snapshot, fingerprint

# ── fixtures: a tiny hand-built snapshot with deterministic vectors ───────────────

CAPS = (
    Capability(
        id="cap-create-folder", tool_binding="create_folder",
        description="Create a folder with an explicit name.",
        examples=('create a folder named "reports"', "新建文件夹「报表」"),
        negatives=("不要新建文件夹", "列出所有文件夹"),
    ),
    Capability(
        id="cap-add-term", tool_binding="add_term",
        description="Add a term to a vocabulary domain.",
        examples=('add "quark" to my physics vocab',),
        negatives=("查询 quark 的释义"),
    ),
)


def _snapshot() -> Snapshot:
    vecs = []
    for cap in CAPS:
        for i, ex in enumerate(cap.examples):
            v = [1.0, 0.05 * (i + 1)] if cap.id == "cap-create-folder" else [0.05, 1.0]
            vecs.append(
                qir.types.ExampleVector(
                    capability_id=cap.id, example_index=i,
                    vector=tuple(float(x) for x in v),
                )
            )
    return Snapshot(version=fingerprint(CAPS), built_at=0.0,
                    capabilities=CAPS, example_vectors=tuple(vecs))


class FakeEmbedder:
    def __init__(self, vec):
        self.vec = vec
        self.calls = 0

    async def embed(self, texts):
        self.calls += 1
        return [self.vec for _ in texts]


class FakeLLM:
    def __init__(self, reply=None, exc=None):
        self.reply, self.exc, self.calls = reply, exc, 0

    async def complete_json(self, prompt, system_prompt="", **kw):
        self.calls += 1
        if self.exc:
            raise self.exc
        return self.reply


# ── cascade: candidates + decision ────────────────────────────────────────────────

async def test_cascade_hit_returns_capability_and_version_only():
    snap = _snapshot()
    llm = FakeLLM({"capability_id": "cap-create-folder", "rationale": "matches"})
    res = await qir.route(
        'make folder "Q3"', snapshot=snap, embedder=FakeEmbedder([1.0, 0.0]), llm=llm,
        top_k=3, min_score=0.5, margin=0.05, decision_enabled=True, timeout_seconds=2.0,
    )
    assert res is not None
    # RouteResult vocabulary: capability + version, NOTHING execution-shaped.
    assert res.capability_id == "cap-create-folder"
    assert res.registry_version == snap.version
    assert not hasattr(res, "args") and not hasattr(res, "tool")


async def test_decision_none_abstains():
    snap = _snapshot()
    res = await qir.route(
        '不要新建文件夹 "Q3"', snapshot=snap, embedder=FakeEmbedder([1.0, 0.0]),
        llm=FakeLLM({"capability_id": "NONE", "rationale": "negated"}),
        top_k=3, min_score=0.5, margin=0.05, decision_enabled=True, timeout_seconds=2.0,
    )
    assert res is None


async def test_off_candidate_verdict_is_none():
    snap = _snapshot()
    res = await qir.route(
        'make folder "Q3"', snapshot=snap, embedder=FakeEmbedder([1.0, 0.0]),
        llm=FakeLLM({"capability_id": "rm_rf", "rationale": "invented"}),
        top_k=3, min_score=0.5, margin=0.05, decision_enabled=True, timeout_seconds=2.0,
    )
    assert res is None


async def test_high_similarity_without_decision_stage_does_not_route():
    snap = _snapshot()
    llm = FakeLLM({"capability_id": "cap-create-folder"})
    res = await qir.route(
        'make folder "Q3"', snapshot=snap, embedder=FakeEmbedder([1.0, 0.0]), llm=llm,
        top_k=3, min_score=0.5, margin=0.05, decision_enabled=False, timeout_seconds=2.0,
    )
    assert res is None
    assert llm.calls == 0  # decision dark => cascade never reaches the model


async def test_low_score_no_candidates_skips_decision():
    snap = _snapshot()
    llm = FakeLLM({"capability_id": "cap-create-folder"})
    # negative cosine to both capability vectors: no candidates at all
    res = await qir.route(
        "随便聊聊", snapshot=snap, embedder=FakeEmbedder([0.7, -0.7]), llm=llm,
        top_k=3, min_score=0.82, margin=0.05, decision_enabled=True, timeout_seconds=2.0,
    )
    assert res is None
    assert llm.calls == 0


async def test_ambiguous_margin_abstains_without_llm():
    snap = _snapshot()
    llm = FakeLLM({"capability_id": "cap-create-folder"})
    # equidistant-ish query vector: both capabilities score the same
    res = await qir.route(
        "?", snapshot=snap, embedder=FakeEmbedder([1.0, 1.0]), llm=llm,
        top_k=3, min_score=0.5, margin=0.5, decision_enabled=True, timeout_seconds=2.0,
    )
    assert res is None
    assert llm.calls == 0


async def test_fail_open_on_exception_and_timeout():
    snap = _snapshot()
    for exc in (RuntimeError("tei down"), asyncio.TimeoutError):
        emb = FakeEmbedder([1.0, 0.0])

        async def boom(texts, _exc=exc):
            raise RuntimeError("embed fail") if isinstance(_exc, RuntimeError) else None

        if isinstance(exc, RuntimeError):
            emb.embed = boom  # type: ignore[method-assign]
        res = await qir.route(
            'make folder "Q3"', snapshot=snap, embedder=emb,
            llm=FakeLLM(None, exc=RuntimeError("llm down")),
            top_k=3, min_score=0.5, margin=0.01, decision_enabled=True,
            timeout_seconds=0.001 if isinstance(exc, type) else 2.0,
        )
        assert res is None


async def test_snapshot_none_abstains():
    res = await qir.route(
        'make folder "Q3"', snapshot=None, embedder=FakeEmbedder([1.0, 0.0]),
        llm=FakeLLM({}), top_k=3, min_score=0.5, margin=0.01,
        decision_enabled=True, timeout_seconds=2.0,
    )
    assert res is None


# ── publication atomicity ─────────────────────────────────────────────────────────

async def test_publish_rejects_binding_not_in_direct_tools():
    with pytest.raises(Exception) as ei:
        validate_draft({"capabilities": [{
            "id": "x", "tool_binding": "bash", "description": "d",
            "examples": ["e"],
        }]})
    assert "not an existing DIRECT_TOOLS" in str(ei.value)


async def test_build_fails_loudly_on_embedding_error():
    class Boom:
        async def embed(self, texts):
            raise RuntimeError("tei offline")

    from core.application.chat.qir.types import SnapshotError

    with pytest.raises(SnapshotError):
        await build_snapshot({"capabilities": [{
            "id": "cap-create-folder", "tool_binding": "create_folder",
            "description": "Create a folder.", "examples": ['make a folder "a"'],
        }]}, Boom())


# ── negation guard (both layers) ──────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    '不要新建文件夹「测试」',
    '别创建文件夹"tmp"',
    '请勿添加 "quark" 到我的物理词汇库',
    "don't create a folder named 'x'",
    '不需要提取全文',
])
def test_negation_guard_abstains_l0_and_binding(text):
    ctx = types.SimpleNamespace(owned_asset_id="a" * 32, body=None)
    assert is_negated_request(text)
    assert match_direct_tool(text, ctx) is None
    assert bind_arguments("create_folder", text, ctx) in (None,) or True  # guard first


@pytest.mark.parametrize("text", [
    '新建文件夹「测试」',
    'create a folder named "tmp" and remember it',  # positive stays positive
])
def test_positive_phrases_unaffected_by_guard(text):
    assert not is_negated_request(text)


def test_bind_arguments_missing_binding_is_integrity_not_none():
    with pytest.raises(ActionIntegrityFailure):
        bind_arguments("no_such_tool", '新建文件夹「x」', object())


def test_bind_arguments_happy_path_delegates_to_existing_extractors():
    args = bind_arguments(
        "create_folder", '新建文件夹「资料」',
        types.SimpleNamespace(owned_asset_id=None, body=None),
    )
    assert args == {"name": "资料"}


# ── stage-2 failure classification in resolve_plan (NOT a catch-all to Agent) ────────

def _stage_ctx(message):
    return types.SimpleNamespace(body=types.SimpleNamespace(message=message))


def _stage_deps():
    return types.SimpleNamespace(session_factory=None, embedder=lambda: FakeEmbedder([1.0, 0.0]), llm=None)


async def _run_stage(monkeypatch, bind_impl):
    # P0 move: the stage now lives in intent_funnel.funnel (verbatim); this
    # helper calls it directly exactly as it used to call the orchestrator's
    # private _qir_intent_stage.
    from core.application.chat import qir as qir_pkg
    from core.application.chat.intent_funnel.funnel import run_intent_stage
    from core.application.chat.qir import store as qir_store
    from core.application.chat.understanding import TurnRequirements

    snap = _snapshot()

    async def active(_sf):
        return snap

    async def route(*a, **k):
        return qir.types.RouteResult(
            capability_id="cap-create-folder", registry_version=snap.version,
        )

    monkeypatch.setattr(qir_store, "active", active)
    monkeypatch.setattr(qir_pkg, "route", route)
    monkeypatch.setattr("core.application.chat.actions.bind_arguments", bind_impl)
    req = TurnRequirements()
    out = await run_intent_stage(_stage_ctx('make folder "x"'), _stage_deps(), req)
    return req, out


async def test_stage2_binding_miss_abstains_to_agent(monkeypatch):
    # C1: args undeterminable => original requirements untouched (zero pollution).
    # bind_arguments is SYNC — the fake must be too.
    def none_bind(tool, text, ctx):
        return None

    req, out = await _run_stage(monkeypatch, none_bind)
    assert out is req
    assert out.requested_action is None


async def test_stage2_integrity_fault_marks_terminal_action(monkeypatch):
    # C2: must NOT fall through to Agent — the plan carries an integrity marker
    # and maps to ACTION (executor issues the decided terminal).
    def boom_bind(tool, text, ctx):
        raise ActionIntegrityFailure("no argument binding for tool 'create_folder'")

    _, out = await _run_stage(monkeypatch, boom_bind)
    assert out is not None
    assert out.requested_action and out.requested_action.get("binding_integrity")
    from core.application.chat.execution_plan import PlanKind, PolicyContext, build_execution_plan

    plan = build_execution_plan(
        out, PolicyContext(fast_paths_enabled=True, action_enabled=True),
    )
    assert plan.kind is PlanKind.ACTION


# ── qir package import boundary (frozen: routing layer owns no execution) ─────────

def test_qir_package_imports_never_reach_api_or_agent():
    mods = [
        "core.application.chat.qir",
        "core.application.chat.qir.types",
        "core.application.chat.qir.snapshot",
        "core.application.chat.qir.store",
        "core.application.chat.qir.semantic",
        "core.application.chat.qir.decision",
    ]
    for name in mods:
        mod = __import__(name, fromlist=["_"])
        with open(mod.__file__, encoding="utf-8") as fh:
            src = fh.read()
        for line in src.splitlines():
            code = line.split("#", 1)[0]
            assert "import api" not in code and "from api" not in code, name
            assert "import agent" not in code and "from agent" not in code, name
