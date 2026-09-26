"""P4 — the operations plane: preview, TOCTOU unification, routing events.

Pinning the P4 rulings:

* 8.5: the console can dry-run ONE query through the WHOLE chain
  (Registry → Matcher → Recall → ToolIntentModel → Binder → Final Route)
  with zero side effects — run_tool is not even on the preview object graph;
* the preview is not gated by ``chat_funnel_enabled`` (the production gate
  gates PRODUCTION traffic; an admin console must be able to inspect the dark
  lane), but it IS gated per-kind exactly like routing (入表≠开闸 holds);
* 8.14: every embedding/LLM call a preview makes is billed
  ``execution_mode=preview``; the pin resets exception-included;
* 8.12: one event row per route (production and preview), best-effort — a
  telemetry fault never sinks a turn;
* TOCTOU (8.9) speaks the ROUTER's namespace: a funnel-certified turn
  re-validates against the Registry content fingerprint — the live tables are
  the only routing namespace (the QIR index-version branch was deleted by
  migration 0014);
* a kind gate flipped OFF between routing and dispatch kills the dispatch —
  the rollout promise holds at the side-effect boundary too.
"""
from __future__ import annotations

import re
import types

import pytest
from core.application.chat.intent_funnel import funnel
from core.application.chat.intent_funnel.contract import (
    REASON_KIND_DISABLED,
    REASON_NO_CANDIDATE,
    REASON_REGISTRY_UNAVAILABLE,
)
from core.application.chat.intent_funnel.registry import content_fingerprint
from core.application.chat.intent_funnel.registry.entry import (
    KIND_ACTION,
    KIND_PRIVATE,
    CapabilityEntry,
    QueryRecord,
    RegistryLiveView,
    derive_language,
)

MSG = '新建文件夹"季度报告"'


def _entry(cid, *, tool="create_folder", corpus=(), patterns=(), aliases=(),
           kind=KIND_ACTION, description=None, **kw):
    # corpus = the exact-set sentences (Standard + Similar live rows)
    corpus = tuple(corpus)
    std = (QueryRecord(id="q1", query=corpus[0],
                       language=derive_language(corpus[0])),) if corpus else ()
    sims = tuple(QueryRecord(id=f"q{10 + n}", query=s, language=derive_language(s),
                             position=n, standard_query_id="q1" if std else None)
                 for n, s in enumerate(corpus[1:]))
    return CapabilityEntry(
        capability_id=cid, tool_binding=tool,
        description=description or f"does {cid}",
        standard_queries=std, similar_queries=sims,
        patterns=tuple(patterns), aliases=tuple(aliases),
        parameters={"name": {"type": "string", "required": True,
                             "max_len": 120, "description": "folder name"}},
        arg_slots={"name": {"source": "user_input"}},
        intent_kind=kind, **kw,
    )


def _view(entries, version=1):
    return RegistryLiveView(
        fingerprint=content_fingerprint(list(entries)),
        entries=tuple(entries),
    )


def _ctx(msg, session_id="s-1", **body_kw):
    return types.SimpleNamespace(
        body=types.SimpleNamespace(message=msg, attach=body_kw.get("attach"),
                                   viewer=body_kw.get("viewer")),
        owned_asset_id=None, research_turn=False, effective_handoff=None,
        session_id=session_id,
    )


def _index():
    return types.SimpleNamespace(version="corpus1-test", corpus=())


class _Embedder:
    def __init__(self, seen=None):
        self.seen = seen if seen is not None else []

    async def embed(self, texts):
        from core.infrastructure.request_context import get_request_execution_mode

        self.seen.append(get_request_execution_mode())
        return [[1.0, 0.0] for _ in texts]


def _open(monkeypatch, *, mode="off", private=False, funnel_on=True):
    from core.config import settings

    monkeypatch.setattr(settings, "chat_funnel_enabled", funnel_on)
    monkeypatch.setattr(settings, "chat_fast_paths_enabled", True)
    monkeypatch.setattr(settings, "chat_action_fast_path_enabled", True)
    monkeypatch.setattr(settings, "chat_matcher_mode", mode)
    # certification lanes need real argument drafts: ride the online seam with
    # the scripted ToolIntentModel double (the stub's zero extraction power is pinned
    # in test_funnel_p2; here the ops plane is the subject)
    monkeypatch.setattr(settings, "chat_tool_intent_backend", "online")
    monkeypatch.setattr(settings, "chat_tool_intent_min_confidence", 0.75)
    monkeypatch.setattr(settings, "chat_tool_intent_online_model", "")
    monkeypatch.setattr(settings, "chat_tool_intent_timeout_seconds", 4.0)
    monkeypatch.setattr(settings, "chat_funnel_timeout_seconds", 5.0)
    monkeypatch.setattr(settings, "chat_funnel_private_enabled", private)


class _ScriptedToolIntent:
    """Deterministic single-hop double: one card -> select it and quote-strip
    the name slot; a split card set -> the honest NONE."""

    async def complete_json(self, prompt, **kw):
        caps = re.findall(r"(?m)^### (\S+)$", prompt)
        if len(caps) != 1:
            return {"capability_id": "NONE", "confidence": 1.0, "arguments": {}}
        m = re.search(r"<user_sentence>(.*?)</user_sentence>", prompt, re.DOTALL)
        quoted = re.search(r'"([^"]+)"', m.group(1) if m else "")
        args = {"name": quoted.group(1)} if quoted else {}
        return {"capability_id": caps[0], "confidence": 0.95, "arguments": args}


class _FakeSession:
    def __init__(self, rows):
        self.rows = rows
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def add(self, obj):
        self.rows.append(obj)

    async def commit(self):
        self.committed = True


def _wire(monkeypatch, *, view, index=None, embedder=None, llm=None,
          session_factory=None):
    async def fake_active(**kw):
        return view

    async def fake_load(sf):
        return index if index is not None else _index()

    monkeypatch.setattr(
        "core.application.chat.intent_funnel.registry.active_view", fake_active)
    monkeypatch.setattr(
        "core.application.chat.intent_funnel.recall.load_index", fake_load)
    return types.SimpleNamespace(
        session_factory=session_factory,
        embedder=lambda: (embedder or _Embedder()),
        llm=llm if llm is not None else _ScriptedToolIntent(),
    )


# ════════════════════════ 8.5: the full-chain query preview ═════════════════════


async def test_preview_certifies_and_reports_the_whole_chain(monkeypatch):
    _open(monkeypatch, mode="on")
    view = _view([_entry("cap-a", corpus=(MSG,))])
    deps = _wire(monkeypatch, view=view)
    res = await funnel.preview(MSG, deps=deps)
    assert res["final_route"] == "action"
    assert res["deepest_stage"] == "certified"
    assert res["execution_mode"] == "preview"
    assert res["registry_version"] == view.fingerprint
    # E2 (final semantics 2026-09-26): an exact HIT never touches the Recall
    # index — the HIT-lane preview/report carries NO index version.
    assert res["index_version"] == "-"
    assert res["route"]["capability_id"] == "cap-a"
    assert res["route"]["tool"] == "create_folder"
    assert res["route"]["args"] == {"name": "季度报告"}
    # single-hop chain: every certified lane exits through the ToolIntentModel stage
    # (the old "matcher" direct-certification stage is deleted)
    assert res["route"]["funnel_stage"] == "tool_intent"
    assert res["route"]["funnel_kind"] == KIND_ACTION


async def test_preview_abstains_with_the_reason_and_no_route(monkeypatch):
    _open(monkeypatch, mode="off")
    view = _view([_entry("cap-a", corpus=(MSG,))])
    deps = _wire(monkeypatch, view=view)
    res = await funnel.preview("完全无关的一句话", deps=deps)
    assert res["final_route"] == "agent"
    assert res["fallback_reason"] == REASON_NO_CANDIDATE
    assert "route" not in res


async def test_preview_ignores_the_production_gate(monkeypatch):
    # an admin must be able to dry-run the DARK lane: the funnel gate scopes
    # production traffic, not the console.
    _open(monkeypatch, mode="on", funnel_on=False)
    view = _view([_entry("cap-a", corpus=(MSG,))])
    deps = _wire(monkeypatch, view=view)
    res = await funnel.preview(MSG, deps=deps)
    assert res["final_route"] == "action"


async def test_preview_honors_the_kind_gate(monkeypatch):
    _open(monkeypatch, mode="on", private=False)
    view = _view([_entry("cap-p", corpus=(MSG,), kind=KIND_PRIVATE)])
    deps = _wire(monkeypatch, view=view)
    res = await funnel.preview(MSG, deps=deps)
    assert res["final_route"] == "agent"
    assert res["fallback_reason"] == REASON_KIND_DISABLED
    # opening the gate flips ONLY the kind verdict — same query, same table
    _open(monkeypatch, mode="on", private=True)
    res2 = await funnel.preview(MSG, deps=deps)
    assert res2["final_route"] == "action"
    assert res2["route"]["funnel_kind"] == KIND_PRIVATE


async def test_preview_pins_execution_mode_and_always_resets(monkeypatch):
    from core.infrastructure.request_context import get_request_execution_mode

    _open(monkeypatch, mode="off")
    view = _view([_entry("cap-a", corpus=(MSG,))])
    seen = []
    deps = _wire(monkeypatch, view=view, embedder=_Embedder(seen))
    res = await funnel.preview("查一查", deps=deps)
    assert res["final_route"] == "agent"
    assert seen and all(m == "preview" for m in seen)   # the spend was preview usage
    assert get_request_execution_mode() == "production"  # ... and the pin died with it


async def test_preview_fail_open_on_registry_fault(monkeypatch):
    _open(monkeypatch, mode="off")

    async def boom(**kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(
        "core.application.chat.intent_funnel.registry.active_view", boom)
    deps = types.SimpleNamespace(session_factory=None,
                                 embedder=lambda: _Embedder(), llm=None)
    res = await funnel.preview(MSG, deps=deps)   # must NOT raise
    assert res["final_route"] == "agent"
    assert res["fallback_reason"] == REASON_REGISTRY_UNAVAILABLE


async def test_preview_cannot_reach_the_tool_runtime(monkeypatch):
    # 8.8 by construction: the funnel object graph has no run_tool at all.
    _open(monkeypatch, mode="on")
    view = _view([_entry("cap-a", corpus=(MSG,))])
    deps = _wire(monkeypatch, view=view)
    assert not hasattr(deps, "run_tool")
    res = await funnel.preview(MSG, deps=deps)
    assert res["route"]["tool"] == "create_folder"  # named, never called


# ════════════════════════ 8.12: one event row per route ═════════════════════════


async def test_certified_turn_writes_production_event(monkeypatch):
    _open(monkeypatch, mode="on")
    view = _view([_entry("cap-a", corpus=(MSG,))])
    rows = []
    deps = _wire(monkeypatch, view=view, session_factory=lambda: _FakeSession(rows))
    from core.application.chat.understanding import (
        Complexity,
        Confidence,
        Signal,
        TurnRequirements,
    )
    requirements = TurnRequirements(
        complexity=Complexity.LOW, confidence=Confidence.LOW,
        needs_web=Signal.LOW, needs_memory=False,
    )
    out = await funnel.route(_ctx(MSG), deps=deps, requirements=requirements)
    assert out is not requirements                      # certified
    assert len(rows) == 1                               # one row, one route
    ev = rows[0]
    assert ev.execution_mode == "production"
    assert ev.final_route == "action" and ev.capability_id == "cap-a"
    assert ev.session_id == "s-1"
    assert ev.registry_version == view.fingerprint
    # E2: this certified turn rode the HIT lane — no Recall index was loaded,
    # so the event row honestly records no index version.
    assert ev.index_version == "-"
    assert ev.total_ms >= 0


async def test_abstain_writes_the_fallback_event(monkeypatch):
    _open(monkeypatch, mode="off")
    view = _view([_entry("cap-a", corpus=(MSG,))])
    rows = []
    deps = _wire(monkeypatch, view=view, session_factory=lambda: _FakeSession(rows))
    from core.application.chat.understanding import (
        Complexity,
        Confidence,
        Signal,
        TurnRequirements,
    )
    requirements = TurnRequirements(
        complexity=Complexity.LOW, confidence=Confidence.LOW,
        needs_web=Signal.LOW, needs_memory=False,
    )
    out = await funnel.route(_ctx("无关句子"), deps=deps, requirements=requirements)
    assert out is requirements
    assert rows[0].final_route == "agent"
    assert rows[0].fallback_reason == REASON_NO_CANDIDATE


async def test_event_write_failure_never_sinks_the_turn(monkeypatch):
    _open(monkeypatch, mode="on")
    view = _view([_entry("cap-a", corpus=(MSG,))])

    def angry_factory():
        raise RuntimeError("telemetry db on fire")

    deps = _wire(monkeypatch, view=view, session_factory=angry_factory)
    from core.application.chat.understanding import (
        Complexity,
        Confidence,
        Signal,
        TurnRequirements,
    )
    requirements = TurnRequirements(
        complexity=Complexity.LOW, confidence=Confidence.LOW,
        needs_web=Signal.LOW, needs_memory=False,
    )
    out = await funnel.route(_ctx(MSG), deps=deps, requirements=requirements)
    assert out.requested_action["capability_id"] == "cap-a"  # the turn stands


async def test_preview_event_lands_with_preview_mode(monkeypatch):
    _open(monkeypatch, mode="on")
    view = _view([_entry("cap-a", corpus=(MSG,))])
    rows = []
    deps = _wire(monkeypatch, view=view, session_factory=lambda: _FakeSession(rows))
    await funnel.preview(MSG, deps=deps)
    assert rows[0].execution_mode == "preview"
    assert rows[0].session_id is None            # console runs belong to no session


# ════════════════ TOCTOU unification (8.9): the router's namespace ══════════════


def _action(view, *, kind=KIND_ACTION, tool="create_folder"):
    # the _certified action shape (live-table ruling): the ONE Registry stamp is
    # funnel_registry_version — the legacy "registry_version" key no longer exists
    return {
        "tool": tool, "args": {"name": "x"}, "capability_id": "cap-a",
        "funnel_registry_version": view.fingerprint,
        "funnel_stage": "tool_intent", "funnel_kind": kind,
    }


def _exec_req(action, run_tool, session_factory, *, roster=None):
    from core.application.chat.execution_plan import ExecutionPlan, PlanKind
    from core.application.chat.executors.action import ActionExecutor
    from core.application.chat.executors.base import ChatDeps, TurnRequest

    class _SM:
        async def append_message(self, role, content):
            pass

    ctx = types.SimpleNamespace(user_text=MSG, session_memory=_SM(), history=[])
    # the executor's tool-existence/schema truth is deps.agent.runtime.schemas()
    # (ruling 2026-09-26); the unit world fakes it — default carries create_folder.
    schemas = lambda: (_DEFAULT_ROSTER if roster is None else roster)
    kernel = types.SimpleNamespace(
        runtime=types.SimpleNamespace(schemas=schemas))
    deps = ChatDeps(
        session_factory=session_factory, queue=None, drive=None, agent=kernel,
        llm=None, embedder=None, viewer=None, new_approval_bridge=None,
        persist_turn_meta=None, log_usage=None, resolve_research=None,
        run_tool=run_tool,
    )
    plan = ExecutionPlan(kind=PlanKind.ACTION, action=action)
    return ActionExecutor(), TurnRequest(ctx=ctx, deps=deps, plan=plan)


_DEFAULT_ROSTER = [
    {"name": "create_folder", "description": "d",
     "parameters": {"type": "object",
                    "properties": {"name": {"type": "string", "maxLength": 120}},
                    "required": ["name"]}},
]


async def _patch_view(monkeypatch, view):
    async def fake_active(**kw):
        return view

    monkeypatch.setattr(
        "core.application.chat.intent_funnel.registry.active_view", fake_active)


async def test_funnel_stamped_dispatch_ok_when_view_unchanged(monkeypatch):
    _open(monkeypatch, mode="on")
    view = _view([_entry("cap-a", corpus=(MSG,))])
    await _patch_view(monkeypatch, view)
    calls = []

    async def run_tool(tool, args, ctx):
        calls.append(tool)
        return {"ok": True, "output": "done"}

    ex, req = _exec_req(_action(view), run_tool, session_factory=None)
    assert await ex._dispatch(req) == "done"
    assert calls == ["create_folder"]


async def test_fingerprint_drift_is_terminal_stale(monkeypatch):
    _open(monkeypatch, mode="on")
    routed = _view([_entry("cap-a", corpus=(MSG,))], version=1)
    drifted = _view([_entry("cap-a", corpus=(MSG,), description="changed"),
                     _entry("cap-b")], version=2)
    await _patch_view(monkeypatch, drifted)
    calls = []

    async def run_tool(tool, args, ctx):
        calls.append(tool)
        return {"ok": True}

    ex, req = _exec_req(_action(routed), run_tool, session_factory=None)
    from core.application.chat.executors.action import _TERMINAL_STALE_ROUTE
    assert await ex._dispatch(req) == _TERMINAL_STALE_ROUTE
    assert calls == []          # never executed: the stale verdict is provably pre-commit


async def test_capability_disabled_mid_air_is_terminal_stale(monkeypatch):
    _open(monkeypatch, mode="on")
    # isolate the ENTRY rule from the fingerprint rule: publish a v2 whose
    # fingerprint we stamp, but whose entry is disabled — dispatch must die.
    after = _view([_entry("cap-a", corpus=(MSG,), enabled=False, status="disabled")])
    await _patch_view(monkeypatch, after)
    from core.application.chat.executors.action import _TERMINAL_STALE_ROUTE

    async def run_tool(*a):
        raise AssertionError("must not execute")

    ex, req = _exec_req(_action(after), run_tool, session_factory=None)
    assert await ex._dispatch(req) == _TERMINAL_STALE_ROUTE


async def test_tool_missing_from_runtime_roster_is_terminal_stale(monkeypatch):
    """Ruling 2026-09-26 (point 3): the drift check runs against the LIVE
    ToolRuntime roster, not the legacy DIRECT_TOOLS table — a stamped route
    whose tool has vanished from the runtime dies pre-commit."""
    _open(monkeypatch, mode="on")
    view = _view([_entry("cap-a", corpus=(MSG,))])
    await _patch_view(monkeypatch, view)
    calls = []

    async def run_tool(tool, args, ctx):
        calls.append(tool)
        return {"ok": True, "output": "done"}

    ex, req = _exec_req(_action(view), run_tool, session_factory=None, roster=[])
    from core.application.chat.executors.action import _TERMINAL_STALE_ROUTE
    assert await ex._dispatch(req) == _TERMINAL_STALE_ROUTE
    assert calls == []


async def test_roster_tool_absent_from_direct_tools_still_dispatches(monkeypatch):
    """…and the converse: a capability bound to a runtime tool the legacy
    DIRECT_TOOLS table never listed dispatches fine — the roster is the truth
    and DIRECT_TOOLS was never expanded for this to work."""
    _open(monkeypatch, mode="on")
    from core.application.chat.intent_funnel.registry.plugins import DIRECT_TOOLS
    assert "list_documents" not in DIRECT_TOOLS
    view = _view([_entry("cap-a", corpus=(MSG,), tool="list_documents")])
    await _patch_view(monkeypatch, view)
    calls = []

    async def run_tool(tool, args, ctx):
        calls.append(tool)
        return {"ok": True, "output": "done"}

    action = dict(_action(view, tool="list_documents"), args={})
    roster = [{"name": "list_documents", "description": "d",
               "parameters": {"type": "object", "properties": {},
                              "required": []}}]
    ex, req = _exec_req(action, run_tool, session_factory=None, roster=roster)
    assert await ex._dispatch(req) == "done"
    assert calls == ["list_documents"]


async def test_private_gate_flipped_off_kills_dispatch(monkeypatch):
    _open(monkeypatch, mode="on", private=True)
    view = _view([_entry("cap-a", corpus=(MSG,), kind=KIND_PRIVATE)])
    await _patch_view(monkeypatch, view)
    calls = []

    async def run_tool(tool, args, ctx):
        calls.append(tool)
        return {"ok": True, "output": "done"}

    ex, req = _exec_req(_action(view, kind=KIND_PRIVATE), run_tool, session_factory=None)
    assert await ex._dispatch(req) == "done"     # gate still open: executes

    _open(monkeypatch, mode="on", private=False)  # the console flips it OFF mid-air
    ex2, req2 = _exec_req(_action(view, kind=KIND_PRIVATE), run_tool, session_factory=None)
    from core.application.chat.executors.action import _TERMINAL_STALE_ROUTE
    assert await ex2._dispatch(req2) == _TERMINAL_STALE_ROUTE
    assert calls == ["create_folder"]            # the flip prevented the SECOND call


async def test_action_without_funnel_stamp_skips_the_registry_check(monkeypatch):
    # QIR retirement (migration 0014): the legacy ``registry_version``/qir_store
    # re-validation branch is GONE — the live view is consulted ONLY for turns
    # the funnel certified (funnel_registry_version present). An L0-native
    # action rides the schema gate alone, never the Registry namespace.
    monkeypatch.setattr(
        "core.application.chat.intent_funnel.registry.active_view",
        lambda **kw: pytest.fail("only funnel-stamped turns consult the view"),
    )

    async def run_tool(tool, args, ctx):
        return {"ok": True, "output": "done"}

    action = {"tool": "create_folder", "args": {"name": "x"},
              "capability_id": "cap-a", "registry_version": "idx-9"}
    ex, req = _exec_req(action, run_tool, session_factory=None)
    assert await ex._dispatch(req) == "done"
