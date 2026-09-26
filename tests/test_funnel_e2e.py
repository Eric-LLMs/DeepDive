"""P4 E2E — the target chain under the REAL authenticated /chat/stream router.

Everything rides production assembly (the p5 harness): httpx → chat router →
TurnOrchestrator.resolve_plan → intent_funnel.route → the four nodes →
ExecutionPlan → ActionExecutor → the REAL ``chat._run_tool`` → ToolRuntime →
sandbox ASK → approval → real tool bodies. Only the outer world is faked
(LLM port, Registry/Index contents, embedder, drive).

The 12 coverage items and where they are pinned:

  1 login          every turn runs through the authenticated dependency;
  2 DB             one chat_funnel_events row per routed turn (8.12);
  3 Intent         plain chat: funnel abstains, Agent keeps the text;
  4 Registry       HIT from the table + fail-open when the view faults;
  5 Recall         paraphrase lane scores through the quality gate;
  6 ToolIntentModel        single hop on BOTH lanes (8.17 ladder: auto falls
                   local(absent)->online): the one call selects AND extracts;
                   dedicated-channel forwarding (timeout guardrail + temp 0)
                   asserted through the router; NONE / off-card escalate;
  7 Binder         validate-only: a CONFIDENT verdict without an extractable
                   argument exits BIND_MISSING and the Agent owns the ask;
                   the stub's zero extraction power is honest through the
                   router (it certifies WHICH, never WITH WHAT);
  8 Runtime        the ASK approval frame surfaces (WRITE not pre-granted);
  9 execution      the tool body really writes (spy) + deterministic confirmation;
 10 fallback       Agent input byte-identical on every abstain (8.10);
 11 failure/timeout fail-open: registry fault / slow embedder never sink a turn;
 12 multi-turn     certified turn then a plain turn in one session: two event
                   rows, routing never leaks state.

Chain ruling 2026-09-24: the active path is Matcher HIT / Recall -> ONE ToolIntentModel
call -> Binder validate -> runtime. No second hop, no Decision node — several
tests pin "exactly one model-A call per routed turn".

Coexistence note: while L0 is in charge the funnel only sees turns L0 abstained
from — so the certified-EXECUTION legs patch ``understanding.match_direct_tool``
to abstain, which is exactly the post-P2-promotion world the design targets
(Matcher replaces L0). Everything else runs with L0 fully live, proving the
byte-identical coexistence contract.
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
    REASON_NO_CANDIDATE,
    REASON_RECALL_TIMEOUT,
    REASON_REGISTRY_UNAVAILABLE,
    REASON_TOOL_INTENT_REJECT,
    REASON_TOOL_INTENT_UNCERTAIN,
)
from core.application.chat.intent_funnel.registry import content_fingerprint
from core.application.chat.intent_funnel.registry.entry import (
    KIND_ACTION,
    CapabilityEntry,
    QueryRecord,
    RegistryLiveView,
    derive_language,
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
SHADOW_LOGGER = "core.application.chat.intent_funnel.shadow"
MSG_FOLDER = '新建文件夹"季度报告"'
MSG_PARAPHRASE = '创建文件夹"资料归档"'
MSG_BARE = "新建文件夹"
MSG_NEGATED = '不要新建文件夹"垃圾堆"'
MSG_COMPOUND = '新建文件夹"季度报告"并把"keystone"加入我的词汇库'
STEP = {"content": ["Agent took over."], "tool_calls": None}


# ── the fake production world ─────────────────────────────────────────────────────

def _std(qid, text):
    return QueryRecord(id=qid, query=text, language=derive_language(text))


def _sim(qid, text, parent):
    return QueryRecord(id=qid, query=text, language=derive_language(text),
                       standard_query_id=parent)


def _entries() -> tuple[CapabilityEntry, ...]:
    return (
        CapabilityEntry(
            capability_id="cap-folder", tool_binding="create_folder",
            description="新建一个带引号名称的文件夹。",
            # exact corpus = the live query rows (ruling 2026-09-26): the
            # canonical phrasings are HIT-able; the stored regex/alias fields
            # are inert legacy storage.
            standard_queries=(_std("s1", MSG_FOLDER),),
            similar_queries=(_sim("m1", MSG_BARE, "s1"), _sim("m2", MSG_COMPOUND, "s1")),
            patterns=("re:新建文件夹",), aliases=(MSG_FOLDER,),
            request_query_examples=(MSG_FOLDER,),
            parameters={"name": {"type": "string", "required": True,
                                 "max_len": 120, "description": "folder name"}},
            arg_slots={"name": {"source": "user_input"}},
            intent_kind=KIND_ACTION,
        ),
        CapabilityEntry(
            capability_id="cap-vocab", tool_binding="add_term",
            description="把一个词加入词汇库。",
            standard_queries=(_std("s2", '把"keystone"加入我的词汇库'),),
            similar_queries=(_sim("m3", MSG_COMPOUND, "s2"),),
            patterns=("re:加入我的.*词汇库",), aliases=(),
            request_query_examples=('把"keystone"加入我的词汇库',),
            parameters={"term": {"type": "string", "required": True,
                                 "max_len": 120, "description": "the term"},
                        "domain": {"type": "string", "required": True,
                                   "max_len": 60, "description": "vocabulary domain"}},
            arg_slots={"term": {"source": "user_input"},
                       "domain": {"source": "user_input"}},
            intent_kind=KIND_ACTION,
        ),
    )


class _Index:
    """The LIVE Recall corpus rows (in-process lane: no session_factory)."""

    version = "corpus1-e2e"

    def __init__(self):
        self.corpus = (
            _row("cap-folder", MSG_FOLDER, [1.0, 0.0]),
            _row("cap-vocab", '把"keystone"加入我的词汇库', [0.0, 1.0]),
        )


def _row(cid, query, vector):
    return type("R", (), {
        "kind": "standard", "query_id": f"row-{cid}", "capability_id": cid,
        "query": query, "language": derive_language(query),
        "standard_query_id": None, "vector": vector,
    })()


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


class ToolIntentDouble:
    """ToolIntentModel's online-backend double (8.17): canned {capability_id,
    confidence, arguments} replies; records the per-call kwargs so the
    dedicated-channel forwarding (timeout/temperature) is assertable through
    the router, and the prompts so "exactly one call per turn" is countable."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0
        self.prompts: list[str] = []
        self.kwargs: list[dict] = []

    async def complete_json(self, prompt, *, system_prompt=None, **kw):
        self.calls += 1
        self.prompts.append(prompt)
        self.kwargs.append(kw)
        return self.replies.pop(0) if self.replies else {}


def _funnel_gates(monkeypatch, *, mode="on", timeout=5.0, tool_intent_backend="stub"):
    monkeypatch.setattr(settings, "chat_funnel_enabled", True)
    monkeypatch.setattr(settings, "chat_matcher_mode", mode)
    monkeypatch.setattr(settings, "chat_tool_intent_backend", tool_intent_backend)
    monkeypatch.setattr(settings, "chat_tool_intent_local_url", "")  # not deployed (8.17 ruling)
    # deterministic dedicated-channel state: unconfigured means "ride the pinned
    # channel" — the doubles' forwarded kwargs must not depend on a dev .env.
    monkeypatch.setattr(settings, "chat_tool_intent_online_model", "")
    monkeypatch.setattr(settings, "chat_tool_intent_online_base_url", "")
    monkeypatch.setattr(settings, "chat_tool_intent_online_api_key", "")
    monkeypatch.setattr(settings, "chat_tool_intent_timeout_seconds", 4.0)
    monkeypatch.setattr(settings, "chat_funnel_timeout_seconds", timeout)
    monkeypatch.setattr(settings, "chat_funnel_min_score", 0.82)
    monkeypatch.setattr(settings, "chat_funnel_margin", 0.06)
    monkeypatch.setattr(settings, "chat_funnel_private_enabled", False)
    monkeypatch.setattr(settings, "chat_funnel_web_enabled", False)


def _wire_world(monkeypatch, *, embedder, view=None, index=True, boom=False):
    if boom:
        async def raising(**kw):
            raise RuntimeError("registry db down")
        monkeypatch.setattr(
            "core.application.chat.intent_funnel.registry.active_view", raising)
    else:
        v = view if view is not None else RegistryLiveView(
            fingerprint=content_fingerprint(list(_entries())),
            entries=_entries())

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
           tool_intent=None, tool_intent_backend="stub", retire=False,
           view=None, boom=False):
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
    if tool_intent is not None:
        monkeypatch.setattr(chat_mod, "llm", tool_intent)
    _funnel_gates(monkeypatch, mode=mode, timeout=timeout,
                  tool_intent_backend=tool_intent_backend)
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
    app, port, spy, db, _emb, _ = _setup(monkeypatch)
    caplog.set_level(logging.INFO, logger=FUNNEL_LOGGER)
    msg = "hello there"
    await sse(app, msg)

    assert port.steps == 1 and spy.folders_created == []       # the Agent kept the turn
    assert port.requests[-1][-1]["content"] == msg             # 8.10 byte-identical
    trace = _trace(caplog)
    assert _field(trace, "matcher") == "MISS:-"
    # ruling 2026-09-26: Matcher MISS + no Recall hit >= the gate is an EMPTY
    # model-facing set — the turn exits honestly at NO_CANDIDATE, no hop spent
    assert _field(trace, "fallback_reason") == REASON_NO_CANDIDATE
    assert _field(trace, "final_route") == "agent"
    evs = _events(db)
    assert len(evs) == 1                                       # 8.12 one row per route
    ev = evs[0]
    assert ev.execution_mode == "production" and ev.final_route == "agent"
    assert ev.fallback_reason == REASON_NO_CANDIDATE
    assert ev.session_id                                        # real turn: session stamped
    assert ev.index_version == "corpus1-e2e"


# ── 4+8+9: Matcher HIT -> one ToolIntentModel call (select+extract) -> executes through the
#    REAL runtime
async def test_matcher_certified_turn_executes_through_sandbox(monkeypatch, caplog):
    jd = ToolIntentDouble([{"capability_id": "cap-folder", "confidence": 0.9,
                       "arguments": {"name": "季度报告"}}])
    app, port, spy, db, emb, _broker = _setup(monkeypatch, retire=True,
                                             tool_intent=jd, tool_intent_backend="online")
    caplog.set_level(logging.INFO, logger=FUNNEL_LOGGER)
    res = await sse(app, MSG_FOLDER)

    assert spy.folders_created == [(str(USER), "季度报告")]     # real tool body, once
    assert "Created folder" in (res.answer or "")               # deterministic confirmation
    assert port.steps == 0 and port.single_shot == 0            # zero Agent LLM on this lane
    assert jd.calls == 1                                        # the ONE ToolIntentModel call
    assert "evidence: exact standard-query match (table)" in jd.prompts[0]  # HIT provenance: label, not score
    assert res.approvals                                        # 8: WRITE surfaced ASK, allowed
    trace = _trace(caplog)
    assert _field(trace, "matcher") == "HIT:cap-folder"
    assert _field(trace, "tool_intent") == "CONFIDENT:cap-folder"
    assert _field(trace, "final_route") == "action"
    assert emb.queries == []                                    # deterministic: no embedding spend
    ev = _events(db)[0]
    assert ev.final_route == "action" and ev.capability_id == "cap-folder"
    assert ev.deepest_stage == "certified"


# ── Phase 6 dark switch: chat_funnel_trace_capture ────────────────────────────────

async def test_trace_capture_lands_only_when_the_switch_is_on(monkeypatch):
    """OFF (default): the event write is byte-identical — trace_json stays None.
    ON: the row carries the rebuilt card summary + query + verdict (never the
    full prompt), from the same capture seam the shadow/preview lanes use."""
    jd = ToolIntentDouble([{"capability_id": "cap-folder", "confidence": 0.9,
                            "arguments": {"name": "季度报告"}}])
    app, _port, _spy, db, _emb, _ = _setup(monkeypatch, retire=True,
                                        tool_intent=jd, tool_intent_backend="online")
    await sse(app, MSG_FOLDER)
    assert _events(db)[0].trace_json is None

    monkeypatch.setattr(settings, "chat_funnel_trace_capture", True)
    jd2 = ToolIntentDouble([{"capability_id": "cap-folder", "confidence": 0.9,
                             "arguments": {"name": "季度报告"}}])
    app2, _, _, db2, _, _ = _setup(monkeypatch, retire=True,
                                   tool_intent=jd2, tool_intent_backend="online")
    await sse(app2, MSG_FOLDER)
    tj = _events(db2)[0].trace_json
    assert tj is not None
    assert tj["query"] == MSG_FOLDER
    hit = tj["candidates"][0]
    assert hit["capability_id"] == "cap-folder" and hit["origin"] == "matcher_hit"
    assert hit["kind"] == "standard"                   # MSG_FOLDER is the Standard row
    assert tj["model_verdict"]["decision"] == "CONFIDENT"
    assert tj["binder_state"] == "COMPLETE"
    assert "entry" in tj and tj["entry"]["tool_binding"] == "create_folder"


# ── 5+6: the Recall lane — no table hit, the example scores, the one call certifies ─

async def test_paraphrase_routes_through_recall_and_tool_intent(monkeypatch, caplog):
    jd = ToolIntentDouble([{"capability_id": "cap-folder", "confidence": 0.9,
                       "arguments": {"name": "资料归档"}}])
    app, port, spy, _db, emb, _ = _setup(monkeypatch, mode="off", retire=True,
                                        tool_intent=jd, tool_intent_backend="online")
    caplog.set_level(logging.INFO, logger=FUNNEL_LOGGER)
    await sse(app, MSG_PARAPHRASE)

    assert spy.folders_created == [(str(USER), "资料归档")]
    assert port.steps == 0
    assert jd.calls == 1
    assert "origin=recall" in jd.prompts[0]                     # same card format, recall origin
    trace = _trace(caplog)
    assert _field(trace, "matcher") == "MISS:-"
    assert _field(trace, "recall_count") == "1"
    assert _field(trace, "tool_intent") == "CONFIDENT:cap-folder"
    assert _field(trace, "final_route") == "action"
    assert emb.queries == [MSG_PARAPHRASE]                      # the spend happened here


# ── 6/7: ToolIntentModel's negative exits — NONE rejects, off-card escalates, one call each ─

@pytest.mark.parametrize(
    "reply,reason",
    [( {"capability_id": "NONE", "confidence": 1.0}, REASON_TOOL_INTENT_REJECT),
     ( {"capability_id": "cap-ghost", "confidence": 0.9}, REASON_TOOL_INTENT_UNCERTAIN)],
    ids=["model-none", "off-card"])
async def test_tool_intent_negative_exits_send_the_turn_to_the_agent(monkeypatch, caplog,
                                                                 reply, reason):
    jd = ToolIntentDouble([reply])
    app, port, spy, _db, _emb, _ = _setup(monkeypatch, tool_intent=jd, retire=True,
                                        tool_intent_backend="online")
    caplog.set_level(logging.INFO, logger=FUNNEL_LOGGER)
    await sse(app, MSG_COMPOUND)

    assert spy.folders_created == [] and spy.terms_added == []  # a split turn executes nothing
    assert port.steps == 1 and port.requests[-1][-1]["content"] == MSG_COMPOUND
    assert jd.calls == 1                                        # no second hop exists to spend
    trace = _trace(caplog)
    assert _field(trace, "fallback_reason") == reason


async def test_ambiguous_choice_without_arguments_exits_bind_missing(monkeypatch, caplog):
    # ToolIntentModel may pick one capability from the matcher-ambiguous pair, but with
    # no argument draft the Binder's schema gate abstains — the Agent owns the
    # half-done compound demand (8.7).
    jd = ToolIntentDouble([{"capability_id": "cap-folder", "confidence": 0.9}])
    app, port, spy, _db, _emb, _ = _setup(monkeypatch, tool_intent=jd, retire=True,
                                        tool_intent_backend="online")
    caplog.set_level(logging.INFO, logger=FUNNEL_LOGGER)
    await sse(app, MSG_COMPOUND)

    assert spy.folders_created == []                            # binder owns the veto
    assert port.steps == 1
    assert _field(_trace(caplog), "fallback_reason") == REASON_BIND_MISSING


# ── 7: Binder's MISSING exit through the REAL router — the stub's zero extraction ───

async def test_stub_hit_without_extraction_escalates_bind_missing(monkeypatch, caplog):
    # backend stub (the transition default): the matcher HIT enters the one
    # ToolIntentModel hop, the stub certifies WHICH but has no argument power, so the
    # Binder's validate exits BIND_MISSING — zero LLM calls on the whole turn.
    app, port, spy, _db, _emb, _ = _setup(monkeypatch)           # L0 live: it abstains here too
    caplog.set_level(logging.INFO, logger=FUNNEL_LOGGER)
    await sse(app, MSG_BARE)

    assert spy.folders_created == []
    assert port.steps == 1 and port.requests[-1][-1]["content"] == MSG_BARE
    trace = _trace(caplog)
    assert _field(trace, "matcher") == "HIT:cap-folder"
    assert _field(trace, "tool_intent") == "CONFIDENT:cap-folder"
    assert _field(trace, "fallback_reason") == REASON_BIND_MISSING


# ── 11: every fault shape fails OPEN — the turn never sinks ────────────────────────

async def test_registry_fault_fails_open_to_the_agent(monkeypatch, caplog):
    app, port, _spy, _db, _emb, _ = _setup(monkeypatch, boom=True)
    caplog.set_level(logging.INFO, logger=FUNNEL_LOGGER)
    msg = "帮我建个东西吧"
    res = await sse(app, msg)

    assert res.answer == "Agent took over." and port.steps == 1
    assert _field(_trace(caplog), "fallback_reason") == REASON_REGISTRY_UNAVAILABLE


async def test_slow_recall_times_out_fails_open(monkeypatch, caplog):
    app, port, _spy, db, _emb, _ = _setup(monkeypatch, timeout=0.05, delay=0.3)
    caplog.set_level(logging.INFO, logger=FUNNEL_LOGGER)
    msg = "随便聊聊"
    res = await sse(app, msg)

    assert res.answer == "Agent took over." and port.steps == 1
    trace = _trace(caplog)
    assert _field(trace, "fallback_reason") == REASON_RECALL_TIMEOUT
    assert _events(db)[0].fallback_reason == REASON_RECALL_TIMEOUT


# ── 12: multi-turn — certified then plain in one session, two honest rows ──────────

async def test_multi_turn_routing_does_not_leak_state(monkeypatch, caplog):
    jd = ToolIntentDouble([{"capability_id": "cap-folder", "confidence": 0.9,
                       "arguments": {"name": "季度报告"}}])
    app, port, spy, db, _emb, _ = _setup(monkeypatch, retire=True,
                                        tool_intent=jd, tool_intent_backend="online")
    session = str(uuid4())
    caplog.set_level(logging.INFO, logger=FUNNEL_LOGGER)
    await sse(app, MSG_FOLDER, session_id=session)
    caplog.clear()
    r2 = await sse(app, "换个话题吧", session_id=session)

    assert spy.folders_created == [(str(USER), "季度报告")]
    assert port.steps == 1 and port.requests[-1][-1]["content"] == "换个话题吧"
    assert r2.answer == "Agent took over."
    assert jd.calls == 1                                        # turn 1's hop was the only
    # spend: turn 2's empty candidate set exits at NO_CANDIDATE before the model
    # (ruling 2026-09-26) — a no-candidate turn costs zero hops.
    evs = _events(db)
    assert len(evs) == 2                                        # one row per routed turn
    assert evs[0].final_route == "action" and evs[0].capability_id == "cap-folder"
    assert evs[1].final_route == "agent"
    assert evs[0].fallback_reason == "-" and evs[1].fallback_reason == REASON_NO_CANDIDATE
    assert evs[0].session_id == evs[1].session_id == session


# ── 6+: the REAL tool-intent ladder (8.17) through the router — auto falls local→online ──

async def test_tool_intent_auto_falls_through_to_online_and_certifies(monkeypatch, caplog):
    # The paraphrase reaches ToolIntentModel with one recall candidate; backend=auto with
    # local undeployed must fall through to online (deps.llm seam), and the
    # dedicated-channel forwarding (timeout guardrail + temperature 0) is what
    # the router really sent.
    jd = ToolIntentDouble([{"capability_id": "cap-folder", "confidence": 0.9,
                       "arguments": {"name": "资料归档"}}])
    app, port, spy, _db, _emb, _ = _setup(monkeypatch, mode="off", retire=True,
                                        tool_intent=jd, tool_intent_backend="auto")
    caplog.set_level(logging.INFO, logger=FUNNEL_LOGGER)
    await sse(app, MSG_PARAPHRASE)

    assert spy.folders_created == [(str(USER), "资料归档")]      # online verdict executed
    assert port.steps == 0
    assert jd.calls == 1                                        # local absent: online served once
    kw = jd.kwargs[0]
    assert kw["timeout"] == settings.chat_tool_intent_timeout_seconds
    assert kw["temperature"] == 0.0
    assert "model" not in kw                                    # unconfigured: rides pinned channel
    assert _field(_trace(caplog), "tool_intent") == "CONFIDENT:cap-folder"


async def test_single_hop_spends_exactly_one_tool_intent_call(monkeypatch, caplog):
    # The old BIND_MISSING -> recheck -> Decision ladder is GONE: a CONFIDENT
    # verdict with no extractable argument exits to the Agent after the ONE
    # ToolIntentModel call — no second hop consults the backend again.
    conf = {"capability_id": "cap-folder", "confidence": 0.9}
    jd = ToolIntentDouble([conf, conf])                              # a spare reply must stay unused
    app, port, spy, _db, _emb, _ = _setup(monkeypatch, mode="off", retire=True,
                                        tool_intent=jd, tool_intent_backend="auto")
    caplog.set_level(logging.INFO, logger=FUNNEL_LOGGER)
    await sse(app, MSG_BARE)

    assert spy.folders_created == []
    assert port.steps == 1 and port.requests[-1][-1]["content"] == MSG_BARE
    assert jd.calls == 1                                        # exactly one hop per turn
    trace = _trace(caplog)
    assert _field(trace, "tool_intent") == "CONFIDENT:cap-folder"
    assert "recheck" not in trace
    assert _field(trace, "fallback_reason") == REASON_BIND_MISSING


async def test_negated_demand_is_missed_before_certification(monkeypatch, caplog):
    # a negated demand is not one of the curated exact sentences, so the table
    # misses it on its own; the 8.1-a guard stays as defense in depth. With the
    # HIT vetoed and recall empty the set is empty -> NO_CANDIDATE short-circuit
    # before any hop (ruling 2026-09-26).
    app, port, spy, _db, _emb, _ = _setup(monkeypatch, retire=True)
    caplog.set_level(logging.INFO, logger=FUNNEL_LOGGER)
    res = await sse(app, MSG_NEGATED)

    assert spy.folders_created == []
    assert port.steps == 1 and port.requests[-1][-1]["content"] == MSG_NEGATED
    trace = _trace(caplog)
    assert _field(trace, "matcher") == "MISS:-"
    assert _field(trace, "fallback_reason") == REASON_NO_CANDIDATE
    assert res.answer == "Agent took over."


# ── shadow tri-state at the router: mode off emits zero observation records ───────

async def test_matcher_mode_off_records_no_shadow_telemetry(monkeypatch, caplog):
    app, _port, _spy, _db, _emb, _ = _setup(monkeypatch, mode="off")
    caplog.set_level(logging.INFO, logger=SHADOW_LOGGER)
    await sse(app, MSG_FOLDER)

    assert not [r for r in caplog.records
                if r.name == SHADOW_LOGGER and "matcher_shadow" in r.getMessage()]
