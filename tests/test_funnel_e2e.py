"""P4 E2E — the target chain under the REAL authenticated /chat/stream router.

Everything rides production assembly (the p5 harness): httpx → chat router →
TurnOrchestrator.resolve_plan → intent_funnel.route → the four nodes →
ExecutionPlan → ActionExecutor → the REAL ``chat._run_tool`` → ToolRuntime →
sandbox ASK → approval → real tool bodies. Only the outer world is faked
(LLM port, Registry/Index contents, embedder, drive).

The 13 coverage items and where they are pinned:

  1 login          every turn runs through the authenticated dependency;
  2 DB             one chat_funnel_events row per routed turn (8.12);
  3 Intent         plain chat: funnel abstains, Agent keeps the text;
  4 Registry       HIT from the table + fail-open when the view faults;
  5 Recall         paraphrase lane scores through the quality gate;
  6 Judge          stub CONFIDENT on a single recall candidate /
                   UNCERTAIN escalation of a matcher split;
  7 Decision       the arbiter runs (NONE and off-card both collapse to Agent);
  8 Binder         HIT + unquotable name -> BIND_MISSING -> Agent asks;
  9 Runtime        the ASK approval frame surfaces (WRITE not pre-granted);
 10 execution      the tool body really writes (spy) + deterministic confirmation;
 11 fallback       Agent input byte-identical on every abstain (8.10);
 12 failure/timeout fail-open: registry fault / slow embedder never sink a turn;
 13 multi-turn     certified turn then a plain turn in one session: two event
                   rows, routing never leaks state.

Coexistence note: while L0 is in charge the funnel only sees turns L0
abstained from, and the Binder shares L0's extractor — so the certified-
EXECUTION legs patch ``understanding.match_direct_tool`` to abstain, which is
exactly the post-P2-promotion world the design targets (Matcher replaces L0).
Everything else runs with L0 fully live, proving the byte-identical
coexistence contract.
"""
from __future__ import annotations

import asyncio
import logging
import re
from uuid import uuid4

import pytest
from api.routers import chat as chat_mod
from core.application.chat import understanding as understanding_mod
from core.application.chat.intent_funnel.contract import (
    REASON_BIND_MISSING,
    REASON_DECISION_NONE,
    REASON_NO_CANDIDATE,
    REASON_RECALL_TIMEOUT,
    REASON_REGISTRY_UNAVAILABLE,
)
from core.application.chat.intent_funnel.registry import content_fingerprint
from core.application.chat.intent_funnel.registry.entry import (
    KIND_ACTION,
    CapabilityEntry,
    RegistryVersionView,
)
from core.config import settings
from core.infrastructure.db import ChatFunnelEventModel

from tests._memory_v2_fakes import Db
from tests.p5_validation._p5_harness import (
    USER,
    FakeSeam,
    ScriptedPort,
    Spy,
    _HttpSession,
    build_app,
    build_kernel,
    sse,
)
from tests.p5_validation.test_p5_smoke import _gate

FUNNEL_LOGGER = "core.application.chat.intent_funnel.funnel"
MSG_FOLDER = '新建文件夹"季度报告"'
MSG_PARAPHRASE = '创建文件夹"资料归档"'
MSG_BARE = "新建文件夹"
MSG_COMPOUND = '新建文件夹"季度报告"并把"keystone"加入我的词汇库'
STEP = {"content": ["Agent took over."], "tool_calls": None}


# ── the fake production world ─────────────────────────────────────────────────────

def _entries() -> tuple[CapabilityEntry, ...]:
    return (
        CapabilityEntry(
            capability_id="cap-folder", tool_binding="create_folder",
            description="新建一个带引号名称的文件夹。",
            patterns=("re:新建文件夹",), aliases=(MSG_FOLDER,),
            examples=(MSG_FOLDER,),
            arg_slots={"name": {"source": "user_input"}},
            intent_kind=KIND_ACTION,
        ),
        CapabilityEntry(
            capability_id="cap-vocab", tool_binding="add_term",
            description="把一个词加入词汇库。",
            patterns=("re:加入我的.*词汇库",), aliases=(),
            examples=('把"keystone"加入我的词汇库',),
            arg_slots={"term": {"source": "user_input"},
                       "domain": {"source": "user_input"}},
            intent_kind=KIND_ACTION,
        ),
    )


class _Index:
    version = "idx-e2e"

    def __init__(self):
        self.capabilities = [
            _Cap("cap-folder", (MSG_FOLDER,)),
            _Cap("cap-vocab", ('把"keystone"加入我的词汇库',)),
        ]
        self.example_vectors = [
            _Vec("cap-folder", 0, [1.0, 0.0]),
            _Vec("cap-vocab", 0, [0.0, 1.0]),
        ]


def _Cap(cid, examples):
    return type("C", (), {"id": cid, "examples": examples})()


def _Vec(cid, i, vector):
    return type("V", (), {"capability_id": cid, "example_index": i,
                          "vector": vector})()


class FunnelEmbed:
    """Vector map: the sanctioned paraphrase lands on cap-folder; unknown
    queries fall to 0.707 (< the 0.82 gate) so Recall never guesses."""

    def __init__(self, *, delay: float = 0.0):
        self.delay = delay
        self.queries: list[str] = []

    async def embed(self, texts):
        self.queries.extend(texts)
        if self.delay:
            await asyncio.sleep(self.delay)
        return [[1.0, 0.0] if t == MSG_PARAPHRASE else [0.7, 0.7]
                for t in texts]


class DecisionDouble:
    """Node 4's LLM: complete_json is the frozen arbiter contract."""

    def __init__(self, cap_id: str):
        self.cap_id = cap_id
        self.calls = 0

    async def complete_json(self, prompt, *, system_prompt=None, **kw):
        self.calls += 1
        return {"capability_id": self.cap_id, "rationale": "e2e double"}


def _funnel_gates(monkeypatch, *, mode="on", timeout=5.0):
    monkeypatch.setattr(settings, "chat_funnel_enabled", True)
    monkeypatch.setattr(settings, "chat_matcher_mode", mode)
    monkeypatch.setattr(settings, "chat_judge_backend", "stub")
    monkeypatch.setattr(settings, "chat_funnel_timeout_seconds", timeout)
    monkeypatch.setattr(settings, "chat_funnel_top_k", 3)
    monkeypatch.setattr(settings, "chat_funnel_min_score", 0.82)
    monkeypatch.setattr(settings, "chat_funnel_margin", 0.06)
    monkeypatch.setattr(settings, "chat_funnel_private_enabled", False)
    monkeypatch.setattr(settings, "chat_funnel_web_enabled", False)
    monkeypatch.setattr(settings, "chat_qir_enabled", False)


def _wire_world(monkeypatch, *, embedder, view=None, index=True, boom=False):
    if boom:
        async def raising(**kw):
            raise RuntimeError("registry db down")
        monkeypatch.setattr(
            "core.application.chat.intent_funnel.registry.active_view", raising)
    else:
        v = view if view is not None else RegistryVersionView(
            version=1, state="active",
            fingerprint=content_fingerprint(list(_entries())),
            capabilities=(), entries=_entries())

        async def fake_active(**kw):
            return v
        monkeypatch.setattr(
            "core.application.chat.intent_funnel.registry.active_view", fake_active)
    if index:
        async def fake_load(sf):
            return _Index()
        monkeypatch.setattr(
            "core.application.chat.intent_funnel.recall.load_index", fake_load)


def _retire_l0(monkeypatch):
    """The post-P2-promotion world: L0's exact pass is gone, the Matcher owns
    ACTION routing (see module docstring for why the E2E needs this)."""
    monkeypatch.setattr(understanding_mod, "match_direct_tool",
                        lambda text, ctx: None)


def _setup(monkeypatch, *, mode="on", timeout=5.0, steps=None, delay=0.0,
           decision=None, retire=False, view=None, boom=False):
    port = ScriptedPort(steps=steps if steps is not None else [STEP])
    spy = Spy()
    kernel, _, _, broker = build_kernel(monkeypatch, port, spy,
                                        broker_mode="allow")
    _gate(monkeypatch, action=True)
    app = build_app(monkeypatch, port, FakeSeam([]), kernel, broker)

    shared = Db(rows=[])
    monkeypatch.setattr(chat_mod, "SessionLocal", lambda: _HttpSession(shared))
    embedder = FunnelEmbed(delay=delay)
    monkeypatch.setattr(chat_mod, "_embedder", lambda: embedder)
    if decision is not None:
        monkeypatch.setattr(chat_mod, "llm", decision)
    _funnel_gates(monkeypatch, mode=mode, timeout=timeout)
    _wire_world(monkeypatch, embedder=embedder, view=view, boom=boom)
    if retire:
        _retire_l0(monkeypatch)
    return app, port, spy, shared, embedder, broker


def _trace(caplog) -> str:
    lines = [r.getMessage() for r in caplog.records
             if r.name == FUNNEL_LOGGER and "funnel_trace" in r.getMessage()]
    assert len(lines) == 1, f"expected one funnel_trace line, got {lines}"
    return lines[0]


def _field(trace: str, name: str) -> str:
    m = re.search(rf"{name}=(\S+)", trace)
    assert m, trace
    return m.group(1)


def _events(db) -> list:
    return [o for o in db.added if isinstance(o, ChatFunnelEventModel)]


# ── 1+2+3+11: plain chat with the gate ON — funnel abstains, Agent verbatim ────────

async def test_plain_chat_abstains_and_lands_one_production_event(monkeypatch, caplog):
    app, port, spy, db, emb, _ = _setup(monkeypatch)
    caplog.set_level(logging.INFO, logger=FUNNEL_LOGGER)
    msg = "hello there"
    await sse(app, msg)

    assert port.steps == 1 and spy.folders_created == []       # the Agent kept the turn
    assert port.requests[-1][-1]["content"] == msg             # 8.10 byte-identical
    trace = _trace(caplog)
    assert _field(trace, "matcher") == "MISS:-"
    assert _field(trace, "fallback_reason") == REASON_NO_CANDIDATE
    assert _field(trace, "final_route") == "agent"
    evs = _events(db)
    assert len(evs) == 1                                       # 8.12 one row per route
    ev = evs[0]
    assert ev.execution_mode == "production" and ev.final_route == "agent"
    assert ev.fallback_reason == REASON_NO_CANDIDATE
    assert ev.session_id                                        # real turn: session stamped
    assert ev.index_version == "idx-e2e"


# ── 4+9+10: Matcher-certified turn executes through the REAL runtime ───────────────

async def test_matcher_certified_turn_executes_through_sandbox(monkeypatch, caplog):
    app, port, spy, db, emb, broker = _setup(monkeypatch, retire=True)
    caplog.set_level(logging.INFO, logger=FUNNEL_LOGGER)
    res = await sse(app, MSG_FOLDER)

    assert spy.folders_created == [(str(USER), "季度报告")]     # real tool body, once
    assert "Created folder" in (res.answer or "")               # deterministic confirmation
    assert port.steps == 0 and port.single_shot == 0            # zero LLM on this lane
    assert res.approvals                                        # 9: WRITE surfaced ASK, allowed
    trace = _trace(caplog)
    assert _field(trace, "matcher") == "HIT:cap-folder"
    assert _field(trace, "final_route") == "action"
    assert emb.queries == []                                    # deterministic: no embedding spend
    ev = _events(db)[0]
    assert ev.final_route == "action" and ev.capability_id == "cap-folder"
    assert ev.deepest_stage == "certified"


# ── 5+6: the Recall lane — no table hit, the example scores, Judge CONFIDENTs ──────

async def test_paraphrase_routes_through_recall_and_judge(monkeypatch, caplog):
    app, port, spy, db, emb, _ = _setup(monkeypatch, mode="off", retire=True)
    caplog.set_level(logging.INFO, logger=FUNNEL_LOGGER)
    await sse(app, MSG_PARAPHRASE)

    assert spy.folders_created == [(str(USER), "资料归档")]
    assert port.steps == 0
    trace = _trace(caplog)
    assert _field(trace, "matcher") == "MISS:-"
    assert _field(trace, "recall_count") == "1"
    assert _field(trace, "judge") == "CONFIDENT:cap-folder"
    assert _field(trace, "final_route") == "action"
    assert emb.queries == [MSG_PARAPHRASE]                      # the spend happened here


# ── 7: the Decision node runs — NONE and off-card both escalate to the Agent ───────

@pytest.mark.parametrize("verdict", ["NONE", "cap-ghost"],
                         ids=["arbiter-none", "off-card"])
async def test_decision_escalation_sends_the_turn_to_the_agent(monkeypatch, caplog,
                                                               verdict):
    dec = DecisionDouble(verdict)
    app, port, spy, db, emb, _ = _setup(monkeypatch, decision=dec, retire=True)
    caplog.set_level(logging.INFO, logger=FUNNEL_LOGGER)
    await sse(app, MSG_COMPOUND)

    assert spy.folders_created == [] and spy.terms_added == []  # a split turn executes nothing
    assert port.steps == 1 and port.requests[-1][-1]["content"] == MSG_COMPOUND
    assert dec.calls == 1                                       # Node 4 was really consulted
    trace = _trace(caplog)
    assert "MATCH_AMBIGUOUS" in _field(trace, "matcher") or "MATCH_AMBIGUOUS" in trace
    assert _field(trace, "decision") == "NONE"
    assert _field(trace, "fallback_reason") == REASON_DECISION_NONE


async def test_decision_pick_executes_only_the_chosen_capability(monkeypatch, caplog):
    # the arbiter CAN route: pick cap-folder among the matcher-ambiguous pair.
    # The Binder then abstains (compound demand) — proving Decision → Binder
    # hand-off without executing half a compound request (8.7 last-question).
    dec = DecisionDouble("cap-folder")
    app, port, spy, db, emb, _ = _setup(monkeypatch, decision=dec, retire=True)
    caplog.set_level(logging.INFO, logger=FUNNEL_LOGGER)
    await sse(app, MSG_COMPOUND)

    assert spy.folders_created == []                            # binder owns the veto
    assert port.steps == 1
    assert _field(_trace(caplog), "fallback_reason") == REASON_BIND_MISSING


# ── 8: Binder's MISSING exit — the table hit but the name is not quotable ──────────

async def test_binder_missing_escalates_with_the_reason(monkeypatch, caplog):
    app, port, spy, db, emb, _ = _setup(monkeypatch)           # L0 live: it abstains here too
    caplog.set_level(logging.INFO, logger=FUNNEL_LOGGER)
    await sse(app, MSG_BARE)

    assert spy.folders_created == []
    assert port.steps == 1 and port.requests[-1][-1]["content"] == MSG_BARE
    trace = _trace(caplog)
    assert _field(trace, "matcher") == "HIT:cap-folder"
    assert _field(trace, "fallback_reason") == REASON_BIND_MISSING


# ── 12: every fault shape fails OPEN — the turn never sinks ────────────────────────

async def test_registry_fault_fails_open_to_the_agent(monkeypatch, caplog):
    app, port, spy, db, emb, _ = _setup(monkeypatch, boom=True)
    caplog.set_level(logging.INFO, logger=FUNNEL_LOGGER)
    msg = "帮我建个东西吧"
    res = await sse(app, msg)

    assert res.answer == "Agent took over." and port.steps == 1
    assert _field(_trace(caplog), "fallback_reason") == REASON_REGISTRY_UNAVAILABLE


async def test_slow_recall_times_out_fails_open(monkeypatch, caplog):
    app, port, spy, db, emb, _ = _setup(monkeypatch, timeout=0.05, delay=0.3)
    caplog.set_level(logging.INFO, logger=FUNNEL_LOGGER)
    msg = "随便聊聊"
    res = await sse(app, msg)

    assert res.answer == "Agent took over." and port.steps == 1
    trace = _trace(caplog)
    assert _field(trace, "fallback_reason") == REASON_RECALL_TIMEOUT
    assert _events(db)[0].fallback_reason == REASON_RECALL_TIMEOUT


# ── 13: multi-turn — certified then plain in one session, two honest rows ──────────

async def test_multi_turn_routing_does_not_leak_state(monkeypatch, caplog):
    app, port, spy, db, emb, _ = _setup(monkeypatch, retire=True)
    session = str(uuid4())
    caplog.set_level(logging.INFO, logger=FUNNEL_LOGGER)
    await sse(app, MSG_FOLDER, session_id=session)
    caplog.clear()
    r2 = await sse(app, "换个话题吧", session_id=session)

    assert spy.folders_created == [(str(USER), "季度报告")]
    assert port.steps == 1 and port.requests[-1][-1]["content"] == "换个话题吧"
    assert r2.answer == "Agent took over."
    evs = _events(db)
    assert len(evs) == 2                                        # one row per routed turn
    assert evs[0].final_route == "action" and evs[0].capability_id == "cap-folder"
    assert evs[1].final_route == "agent"
    assert evs[0].fallback_reason == "-" and evs[1].fallback_reason == REASON_NO_CANDIDATE
    assert evs[0].session_id == evs[1].session_id == session
