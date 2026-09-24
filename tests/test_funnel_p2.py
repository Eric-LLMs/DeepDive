"""P2 — node-level tests + the target cascade through funnel.route.

Discipline (ruling 2026-09-24, §8.17 pipeline doctrine): EVERY node section
below runs against fake contracts only — no node needs another node to be
alive to be tested. Replacing a node's model or thresholds must keep its own
tests passing and leave the other sections byte-unchanged; a test that could
not survive that split is itself an architecture violation.

The cascade section then pins the 8.10 exit classification: on every
abstain/fault the ORIGINAL requirements object is returned BY IDENTITY (the
Agent's turn stays byte-identical), and only a certified turn produces a new
object.
"""
from __future__ import annotations

import asyncio
import logging
import types

import pytest
from core.application.chat.intent_funnel import funnel, guardrails, matcher
from core.application.chat.intent_funnel.contract import (
    BIND_COMPLETE,
    BIND_INVALID,
    BIND_MISSING,
    JUDGE_CONFIDENT,
    JUDGE_REJECT,
    JUDGE_UNCERTAIN,
    MATCH_HIT,
    REASON_BIND_MISSING,
    REASON_DECISION_NONE,
    REASON_NO_CANDIDATE,
    REASON_RECALL_TIMEOUT,
    REASON_RECALL_UNAVAILABLE,
    REASON_REGISTRY_UNAVAILABLE,
    REASON_VERSION_MISMATCH,
    Candidate,
    TurnFacts,
)
from core.application.chat.intent_funnel.judge import base as jbase
from core.application.chat.intent_funnel.registry import content_fingerprint
from core.application.chat.intent_funnel.registry import entry as T
from core.application.chat.understanding import (
    Complexity,
    Confidence,
    Signal,
    TurnRequirements,
)

# ════════════════════════ shared fakes (contract-shaped, minimal) ═══════════════


def _entry(cid, *, tool="create_folder", patterns=(), aliases=(), arg_slots=None,
           enabled=True, status="active", examples=("做个事",), negatives=()):
    return T.CapabilityEntry(
        capability_id=cid, tool_binding=tool, description=f"does {cid}",
        patterns=tuple(patterns), aliases=tuple(aliases), examples=tuple(examples),
        negatives=tuple(negatives), arg_slots=arg_slots if arg_slots is not None
        else {"name": {"source": "user_input"}},
        enabled=enabled, status=status,
    )


def _view(entries, version=1):
    return T.RegistryVersionView(
        version=version, state="active",
        fingerprint=content_fingerprint(list(entries)),
        capabilities=(), entries=tuple(entries),
    )


class _Embedder:
    def __init__(self, vec, *, fail=False):
        self.vec, self.fail, self.calls = list(vec), fail, 0

    async def embed(self, texts):
        self.calls += 1
        if self.fail:
            raise RuntimeError("embedder down")
        return [list(self.vec) for _ in texts]


class _LLM:
    """Replies are popped in order; an Exception member raises (transport fault).
    ``calls`` records the per-call channel kwargs (model/base_url/api_key/timeout/
    temperature) so the online judge's explicit forwarding is assertable."""

    def __init__(self, replies=()):
        self.replies = list(replies)
        self.prompts = []
        self.calls: list[dict] = []

    async def complete_json(self, prompt, *, system_prompt=None, **kw):
        self.prompts.append(prompt)
        self.calls.append(kw)
        if not self.replies:
            return {}
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _index(caps, *, version="idx-9"):
    """caps: [(capability_id, [examples], [example vectors])]. Mirrors the
    published qir-store snapshot shape the real recall consumes."""
    capabilities, vectors = [], []
    for cid, examples, vecs in caps:
        capabilities.append(types.SimpleNamespace(id=cid, examples=tuple(examples)))
        for i, v in enumerate(vecs):
            vectors.append(types.SimpleNamespace(
                capability_id=cid, example_index=i, vector=list(v),
            ))
    return types.SimpleNamespace(
        version=version, capabilities=capabilities, example_vectors=vectors,
    )


def _ctx(msg, **kw):
    base = dict(
        body=types.SimpleNamespace(message=msg, attach=None, viewer=None),
        owned_asset_id=None, research_turn=False, effective_handoff=None,
        session_id="s-1",
    )
    base.update(kw)
    return types.SimpleNamespace(**base)


def _req(**kw):
    base = dict(complexity=Complexity.LOW, confidence=Confidence.LOW,
                needs_web=Signal.LOW, needs_memory=False)
    base.update(kw)
    return TurnRequirements(**base)


# ════════════════════════ TurnFacts (2026-09-24 contract ruling) ════════════════


def test_turn_facts_reads_structured_viewer_and_attach_only():
    viewer = types.SimpleNamespace(asset_id="a-7", page=12,
                                   selections=[types.SimpleNamespace(text="x")])
    ctx = _ctx("总结这一页", body=types.SimpleNamespace(
        message="总结这一页", attach={"asset_id": "b-1"}, viewer=viewer))
    f = TurnFacts.of(ctx)
    assert f == TurnFacts(
        has_viewer=True, viewer_asset_id="a-7", viewer_current_page=12,
        has_viewer_selection=True, has_attachment=True, has_turn_context=True,
    )


def test_turn_facts_plain_turn_is_all_empty():
    f = TurnFacts.of(_ctx("hello", session_id=None))
    assert f.has_viewer is False and f.has_attachment is False
    assert f.viewer_current_page is None and f.has_turn_context is False


def test_matcher_contract_takes_facts_and_never_history():
    v = _view([_entry("cap-a", aliases=("新建文件夹",))])
    facts = TurnFacts(has_viewer=True, viewer_current_page=3)
    # facts is a REQUIRED contract slot; verdicts stay table-only (8.1)
    assert matcher.match("新建文件夹", facts, v).state == MATCH_HIT
    assert matcher.match("新建文件夹", TurnFacts(), v).state == MATCH_HIT


# ═══════════════════════════════ guardrails ═════════════════════════════════════


def test_turn_veto_reasons_and_pass_through():
    req = _req()
    ctx = _ctx("新建文件夹")
    assert guardrails.turn_veto("新建文件夹", req, ctx) is None
    assert guardrails.turn_veto('{"tool": "x"}', req, ctx) == "input_not_pure_text"
    assert guardrails.turn_veto("查一下", _req(needs_web=Signal.HIGH), ctx) \
        == "turn_demands_web_or_memory"
    assert guardrails.turn_veto("查一下", _req(needs_memory=True), ctx) \
        == "turn_demands_web_or_memory"
    assert guardrails.turn_veto("继续", req, _ctx("继续", research_turn=True)) \
        == "context_research_or_handoff"


def test_negation_guard_is_a_pure_predicate():
    assert guardrails.negated("不要新建文件夹")
    assert not guardrails.negated("新建文件夹")


# ═══════════════════════════════ recall (quality gate only) ════════════════════


async def test_recall_scores_gate_truncate_and_never_adjudicates():
    from core.application.chat.intent_funnel import recall as recall_node

    idx = _index([
        ("cap-a", ["做a"], [[1.0, 0.0], [0.9, 0.1]]),   # best-example wins
        ("cap-b", ["做b"], [[0.95, 0.0]]),
        ("cap-c", ["做c"], [[0.1, 0.9]]),                # below min_score
    ])
    res = await recall_node.recall(
        idx, "新建文件夹", embedder=_Embedder([1.0, 0.0]), top_k=2, min_score=0.82,
    )
    ids = [c.capability_id for c in res.candidates]
    assert ids == ["cap-a", "cap-b"]          # ranked, NOT margin-aborted
    assert res.candidates[0].matched_example == "做a"
    assert res.candidates[0].origin == "recall"
    # near-ties survive: recall proposes, the Judge disposes (design §3)


async def test_recall_blank_query_and_embedder_fault():
    from core.application.chat.intent_funnel import recall as recall_node

    idx = _index([("cap-a", ["做a"], [[1.0, 0.0]])])
    assert (await recall_node.recall(idx, "  ", embedder=_Embedder([1.0]),
                                     top_k=3, min_score=0.5)).candidates == ()
    with pytest.raises(RuntimeError):
        await recall_node.recall(idx, "做a", embedder=_Embedder([1.0], fail=True),
                                 top_k=3, min_score=0.5)
    with pytest.raises(RuntimeError):
        await recall_node.recall(idx, "做a", embedder=_Embedder([]),
                                 top_k=3, min_score=0.5)


async def test_recall_refuses_vectors_without_examples():
    from core.application.chat.intent_funnel import recall as recall_node

    idx = _index([("cap-a", ["做a"], [[1.0, 0.0]])])
    idx.example_vectors.append(types.SimpleNamespace(
        capability_id="cap-a", example_index=5, vector=[1.0, 0.0]))  # orphan row
    res = await recall_node.recall(idx, "做a", embedder=_Embedder([1.0, 0.0]),
                                   top_k=3, min_score=0.5)
    assert [c.score for c in res.candidates] == [1.0]  # orphan never scored


# ═══════════════════════════════ judge: stub + ladder ═══════════════════════════


def test_stub_three_states():
    from core.application.chat.intent_funnel.judge import stub

    assert stub.judge((), margin=0.06).decision == JUDGE_REJECT
    one = stub.judge((Candidate("cap-a", 0.91),), margin=0.06)
    assert one.decision == JUDGE_CONFIDENT and one.capability_id == "cap-a"
    m = stub.judge((Candidate("cap-a", 0.0, origin="matcher_ambiguous"),), margin=0.06)
    assert m.decision == JUDGE_UNCERTAIN          # uncalibrated score: no cert
    far = stub.judge((Candidate("cap-a", 0.90), Candidate("cap-b", 0.70)), margin=0.06)
    assert far.decision == JUDGE_CONFIDENT and far.capability_id == "cap-a"
    near = stub.judge((Candidate("cap-a", 0.90), Candidate("cap-b", 0.88)), margin=0.06)
    assert near.decision == JUDGE_UNCERTAIN       # margin below floor: escalate up


async def test_judge_ladder_falls_through_and_sanitizes(monkeypatch):
    from core.application.chat.intent_funnel import judge as judge_pkg
    from core.config import settings

    monkeypatch.setattr(settings, "chat_judge_backend", "local")  # not deployed
    monkeypatch.setattr(settings, "chat_judge_local_url", "")

    cands = (Candidate("cap-a", 0.9),)
    out = await judge_pkg.adjudicate("q", cands, entries_by_id={}, llm=None)
    # local unavailable -> fall through (NOT abstain); no llm -> online also down
    assert out.decision == JUDGE_UNCERTAIN

    monkeypatch.setattr(settings, "chat_judge_backend", "SHTUB")  # typo
    out = await judge_pkg.adjudicate("q", cands, entries_by_id={}, llm=None)
    assert out.decision == JUDGE_CONFIDENT  # unknown backend -> stub, single recall


async def test_judge_reply_discipline(monkeypatch):
    from core.application.chat.intent_funnel import judge as judge_pkg
    from core.config import settings

    monkeypatch.setattr(settings, "chat_judge_min_confidence", 0.75)
    cands = (Candidate("cap-a", 0.9), Candidate("cap-b", 0.5))

    v = judge_pkg._verdict_from_reply({"capability_id": "NONE"}, cands)
    assert v.decision == JUDGE_REJECT
    v = judge_pkg._verdict_from_reply({"capability_id": "cap-z", "confidence": 1.0}, cands)
    assert v.decision == JUDGE_UNCERTAIN              # off-card is never a verdict
    v = judge_pkg._verdict_from_reply({"capability_id": "cap-a", "confidence": 0.4}, cands)
    assert v.decision == JUDGE_UNCERTAIN              # under the floor
    v = judge_pkg._verdict_from_reply({"capability_id": "cap-a", "confidence": 0.9}, cands)
    assert v.decision == JUDGE_CONFIDENT and v.capability_id == "cap-a"


async def test_judge_recheck_stub_cannot_rescue(monkeypatch):
    """8.7: the deterministic stub has no extraction power — a binding problem
    escalates UNRESOLVED (never CONFIDENT, never a fabricated rescue)."""
    from core.application.chat.intent_funnel import judge as judge_pkg
    from core.config import settings

    monkeypatch.setattr(settings, "chat_judge_backend", "stub")
    v = await judge_pkg.recheck("q", "cap-a", entry=_entry("cap-a"),
                                issue="missing", candidates=(Candidate("cap-a", 0.9),))
    assert v.decision == JUDGE_UNCERTAIN and v.capability_id == "cap-a"


def test_judge_backends_raise_unavailable_not_answers():
    from core.application.chat.intent_funnel.judge import local, online

    async def go():
        with pytest.raises(jbase.JudgeUnavailable):
            await local.judge("q", (), {}, url="")
        with pytest.raises(jbase.JudgeUnavailable):
            await online.judge("q", (), {}, llm=None)
        with pytest.raises(jbase.JudgeUnavailable):
            await online.judge("q", (), {}, llm=_LLM([RuntimeError("401")]))
    asyncio.run(go())


# ── 8.17 real-backend wiring (2026-09-24 ruling: online first, dedicated
#    small-model channel with explicit per-call forwarding) ──────────────────────

async def test_judge_online_serves_and_forwards_dedicated_channel(monkeypatch):
    from core.application.chat.intent_funnel import judge as judge_pkg
    from core.config import settings

    monkeypatch.setattr(settings, "chat_judge_backend", "online")
    monkeypatch.setattr(settings, "chat_judge_online_model", "tiny-judge")
    monkeypatch.setattr(settings, "chat_judge_online_base_url", "https://cheap.example/v1")
    monkeypatch.setattr(settings, "chat_judge_online_api_key", "sk-test")
    llm = _LLM([{"capability_id": "cap-a", "confidence": 0.9}])
    out = await judge_pkg.adjudicate(
        "新建文件夹", (Candidate("cap-a", 0.9),), entries_by_id={}, llm=llm)
    assert out.decision == JUDGE_CONFIDENT and out.capability_id == "cap-a"
    kw = llm.calls[0]
    assert kw == {
        "model": "tiny-judge", "base_url": "https://cheap.example/v1",
        "api_key": "sk-test", "timeout": settings.chat_judge_timeout_seconds,
        "temperature": 0.0,
        # 2026-09-24 latency pins (judge call site, not the global knob):
        # reasoning explicitly off + hard output bound for the verdict.
        "max_tokens": 100, "disable_thinking": True,
    }


async def test_judge_online_model_without_endpoint_pair_rides_pinned_channel(monkeypatch):
    """base_url/api_key are honored only as a PAIR: a half-configured dedicated
    endpoint is worse than riding the turn's pinned channel, so only the model
    name is forwarded."""
    from core.application.chat.intent_funnel import judge as judge_pkg
    from core.config import settings

    monkeypatch.setattr(settings, "chat_judge_backend", "online")
    monkeypatch.setattr(settings, "chat_judge_online_model", "tiny-judge")
    monkeypatch.setattr(settings, "chat_judge_online_base_url", "https://cheap.example/v1")
    monkeypatch.setattr(settings, "chat_judge_online_api_key", "")
    llm = _LLM([{"capability_id": "cap-a", "confidence": 0.9}])
    out = await judge_pkg.adjudicate(
        "q", (Candidate("cap-a", 0.9),), entries_by_id={}, llm=llm)
    assert out.decision == JUDGE_CONFIDENT
    assert llm.calls[0].get("model") == "tiny-judge"
    assert "base_url" not in llm.calls[0] and "api_key" not in llm.calls[0]


async def test_judge_auto_local_absent_falls_through_to_online(monkeypatch):
    """8.17 ladder: auto with nothing deployed = local skipped (fall-through,
    never abstain-to-Agent) and the online step really serves."""
    from core.application.chat.intent_funnel import judge as judge_pkg
    from core.config import settings

    monkeypatch.setattr(settings, "chat_judge_backend", "auto")
    monkeypatch.setattr(settings, "chat_judge_local_url", "")
    llm = _LLM([{"capability_id": "cap-a", "confidence": 0.95}])
    out = await judge_pkg.adjudicate(
        "q", (Candidate("cap-a", 0.9),), entries_by_id={}, llm=llm)
    assert out.decision == JUDGE_CONFIDENT and llm.prompts


async def test_judge_auto_full_chain_local_unreachable_online_down_stub_serves(monkeypatch):
    from core.application.chat.intent_funnel import judge as judge_pkg
    from core.config import settings

    monkeypatch.setattr(settings, "chat_judge_backend", "auto")
    # a dead port: transport fault -> JudgeUnavailable -> fall through
    monkeypatch.setattr(settings, "chat_judge_local_url", "http://127.0.0.1:9/v1")
    out = await judge_pkg.adjudicate(
        "q", (Candidate("cap-a", 0.9),), entries_by_id={}, llm=None)
    # local down + online (no llm) down -> the deterministic stub serves
    assert out.decision == JUDGE_CONFIDENT and out.capability_id == "cap-a"


async def test_judge_recheck_auto_without_local_really_calls_online(monkeypatch):
    """The 2026-09-24 recheck fix: auto + no local must try ONLINE, not
    short-circuit to UNCERTAIN the way the old code did."""
    from core.application.chat.intent_funnel import judge as judge_pkg
    from core.config import settings

    monkeypatch.setattr(settings, "chat_judge_backend", "auto")
    monkeypatch.setattr(settings, "chat_judge_local_url", "")
    monkeypatch.setattr(settings, "chat_judge_online_model", "")
    llm = _LLM([{"capability_id": "cap-a", "confidence": 0.9}])
    v = await judge_pkg.recheck("新建文件夹", "cap-a", entry=_entry("cap-a"),
                                issue="missing",
                                candidates=(Candidate("cap-a", 0.9),), llm=llm)
    assert v.decision == JUDGE_CONFIDENT and v.capability_id == "cap-a"
    assert "binding problem: missing" in llm.prompts[0]


def test_local_judge_speaks_openai_wire_or_raises_unavailable(monkeypatch):
    import httpx
    from core.application.chat.intent_funnel.judge import local as local_mod

    seen: dict = {}

    class _Resp:
        def __init__(self, status, body):
            self.status_code, self._body = status, body

        def raise_for_status(self):
            if self.status_code >= 400:
                raise httpx.HTTPStatusError(
                    "boom", request=httpx.Request("POST", "http://j/v1/chat/completions"),
                    response=httpx.Response(self.status_code))

        def json(self):
            return self._body

    class _Client:
        def __init__(self, resp):
            self._resp = resp

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None):
            seen["url"], seen["payload"] = url, json
            return self._resp

    ok_body = {"choices": [{"message": {
        "content": 'verdict: {"capability_id": "cap-a", "confidence": 0.88}'}}]}

    async def go():
        monkeypatch.setattr(local_mod.httpx, "AsyncClient",
                            lambda **kw: _Client(_Resp(200, ok_body)))
        data = await local_mod.judge("q", (), {}, url="http://j/v1/")
        assert data == {"capability_id": "cap-a", "confidence": 0.88}
        assert seen["url"] == "http://j/v1/chat/completions"     # base + wire
        assert seen["payload"]["messages"][0]["role"] == "system"
        assert seen["payload"]["temperature"] == 0.0
        monkeypatch.setattr(local_mod.httpx, "AsyncClient",
                            lambda **kw: _Client(_Resp(503, {})))
        with pytest.raises(jbase.JudgeUnavailable):
            await local_mod.judge("q", (), {}, url="http://j/v1")
        # a 200 whose message is not JSON is UNAVAILABLE, never a verdict
        monkeypatch.setattr(local_mod.httpx, "AsyncClient", lambda **kw: _Client(
            _Resp(200, {"choices": [{"message": {"content": "no json here"}}]})))
        with pytest.raises(jbase.JudgeUnavailable):
            await local_mod.judge("q", (), {}, url="http://j/v1")
    asyncio.run(go())


# ═══════════════════════════════ decision (NONE discipline) ═════════════════════


async def test_decision_verdicts_and_whitelist():
    from core.application.chat.intent_funnel import decision

    cands = (Candidate("cap-a", 0.9),)
    entries = {"cap-a": _entry("cap-a")}

    llm = _LLM([{"capability_id": "cap-a", "rationale": "yes"}])
    r = await decision.adjudicate("新建文件夹", cands, entries_by_id=entries, llm=llm)
    assert r.capability_id == "cap-a"
    p = llm.prompts[0]
    assert "<user_sentence>新建文件夹</user_sentence>" in p        # data, not instructions
    assert "answer NONE" in p and "does: does cap-a" in p         # negatives card

    r = await decision.adjudicate("x", cands, entries_by_id=entries,
                                  llm=_LLM([{"capability_id": "NONE"}]))
    assert r.capability_id is None
    r = await decision.adjudicate("x", cands, entries_by_id=entries,
                                  llm=_LLM([{"capability_id": "cap-off-card"}]))
    assert r.capability_id is None                                # off-card == NONE
    r = await decision.adjudicate("x", cands, entries_by_id=entries,
                                  llm=_LLM([RuntimeError("timeout")]))
    assert r.capability_id is None                                # fault collapses
    r = await decision.adjudicate("x", cands, entries_by_id=entries, llm=None)
    assert r.capability_id is None


# ═══════════════════════════════ binder (four states, 8.7) ═════════════════════


def test_binder_complete_missing_invalid_and_c2():
    from core.application.chat import actions
    from core.application.chat.intent_funnel import binder

    entry = _entry("cap-a", tool="create_folder",
                   arg_slots={"name": {"source": "user_input"}})
    ok = binder.bind(entry, '新建文件夹"季度报告"', _ctx(""))
    assert ok.state == BIND_COMPLETE and ok.args == {"name": "季度报告"}

    miss = binder.bind(entry, "新建文件夹", _ctx(""))          # quoted name missing
    assert miss.state == BIND_MISSING

    wrong = _entry("cap-a", arg_slots={"folder": {"source": "user_input"}})
    bad = binder.bind(wrong, '新建文件夹"季度报告"', _ctx(""))  # whitelist mismatch
    assert bad.state == BIND_INVALID

    long_name = _entry("cap-a", arg_slots={})                   # defaults to schema
    bad = binder.bind(long_name, '新建文件夹"' + "名" * 121 + '"', _ctx(""))
    assert bad.state == BIND_INVALID                            # schema bound

    ghost = _entry("cap-g", tool="ghost_tool")
    with pytest.raises(actions.ActionIntegrityFailure):
        binder.bind(ghost, "新建文件夹", _ctx(""))              # C2 propagates


def test_binder_negation_is_missing_not_answer():
    from core.application.chat.intent_funnel import binder

    entry = _entry("cap-a")
    assert binder.bind(entry, '不要新建文件夹"x"', _ctx("")).state == BIND_MISSING


# ═══════════════════════════════ cascade via route() ═══════════════════════════


def _open(monkeypatch, *, mode="off", timeout=5.0):
    from core.config import settings

    monkeypatch.setattr(settings, "chat_funnel_enabled", True)
    monkeypatch.setattr(settings, "chat_fast_paths_enabled", True)
    monkeypatch.setattr(settings, "chat_action_fast_path_enabled", True)
    monkeypatch.setattr(settings, "chat_matcher_mode", mode)
    monkeypatch.setattr(settings, "chat_judge_backend", "stub")
    monkeypatch.setattr(settings, "chat_funnel_timeout_seconds", timeout)


def _wire(monkeypatch, *, view, index, embedder, llm):
    calls = {"registry": 0}

    async def fake_active(**kw):
        calls["registry"] += 1
        return view

    async def fake_load(sf):
        return index

    monkeypatch.setattr(
        "core.application.chat.intent_funnel.registry.active_view", fake_active)
    monkeypatch.setattr(
        "core.application.chat.intent_funnel.recall.load_index", fake_load)
    deps = types.SimpleNamespace(session_factory=None,
                                 embedder=lambda: embedder, llm=llm)
    return calls, deps


MSG = '新建文件夹"季度报告"'
CAP = [_entry("cap-a", aliases=(MSG,), examples=("建个目录",))]


async def test_gate_closed_cascade_is_physically_dark(monkeypatch):
    from core.config import settings

    monkeypatch.setattr(settings, "chat_matcher_mode", "off")
    monkeypatch.setattr(settings, "chat_qir_enabled", False)
    monkeypatch.setattr(
        "core.application.chat.intent_funnel.registry.active_view",
        lambda **kw: pytest.fail("registry must not be read"),
    )
    req = _req()
    out = await funnel.route(_ctx(MSG), deps=object(), requirements=req)
    assert out is req


async def test_l0_certified_turn_never_enters_the_new_lane(monkeypatch):
    _open(monkeypatch)
    req = _req(requested_action={"tool": "create_folder", "args": {}})
    monkeypatch.setattr(
        "core.application.chat.intent_funnel.registry.active_view",
        lambda **kw: pytest.fail("certified turns bypass the cascade"),
    )
    out = await funnel.route(_ctx(MSG), deps=object(), requirements=req)
    assert out is req


async def test_vetoed_turn_skips_cascade(monkeypatch):
    _open(monkeypatch)
    monkeypatch.setattr(
        "core.application.chat.intent_funnel.registry.active_view",
        lambda **kw: pytest.fail("veto before any cascade read"),
    )
    req = _req(needs_web=Signal.HIGH)
    out = await funnel.route(_ctx(MSG), deps=object(), requirements=req)
    assert out is req


async def test_registry_unavailable_fails_open(monkeypatch, caplog):
    _open(monkeypatch)
    calls, deps = _wire(monkeypatch, view=None, index=None,
                        embedder=_Embedder([1, 0]), llm=_LLM())
    req = _req()
    with caplog.at_level(logging.INFO, logger="core.application.chat.intent_funnel"):
        out = await funnel.route(_ctx(MSG), deps=deps, requirements=req)
    assert out is req and calls["registry"] == 1
    line = next(r.getMessage() for r in caplog.records if "funnel_trace" in r.getMessage())
    assert f"fallback_reason={REASON_REGISTRY_UNAVAILABLE}" in line
    assert "deepest_stage=registry" in line and "final_route=agent" in line


async def test_index_unavailable_and_no_candidate(monkeypatch, caplog):
    _open(monkeypatch)
    req = _req()
    _, deps = _wire(monkeypatch, view=_view(CAP), index=None,
                    embedder=_Embedder([1, 0]), llm=_LLM())
    with caplog.at_level(logging.INFO, logger="core.application.chat.intent_funnel"):
        assert await funnel.route(_ctx(MSG), deps=deps, requirements=req) is req
    assert f"fallback_reason={REASON_RECALL_UNAVAILABLE}" in caplog.records[-1].getMessage()

    empty_idx = _index([])
    _, deps = _wire(monkeypatch, view=_view(CAP), index=empty_idx,
                    embedder=_Embedder([1, 0]), llm=_LLM())
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="core.application.chat.intent_funnel"):
        assert await funnel.route(_ctx("随便聊聊"), deps=deps, requirements=req) is req
    assert f"fallback_reason={REASON_NO_CANDIDATE}" in caplog.records[-1].getMessage()


async def test_matcher_hit_escalates_through_decision_and_certifies(monkeypatch, caplog):
    """mode off: the HIT enters the ladder as an UNCERTAIN (uncalibrated)
    candidate; the Decision LLM certifies; the binder completes."""
    _open(monkeypatch)
    llm = _LLM([{"capability_id": "cap-a", "rationale": "single folder create"}])
    view = _view(CAP)
    idx = _index([("cap-a", ["建个目录"], [[0.1, 0.9]])])  # recall misses the query
    _, deps = _wire(monkeypatch, view=view, index=idx,
                    embedder=_Embedder([1.0, 0.0]), llm=llm)
    req = _req()
    with caplog.at_level(logging.INFO, logger="core.application.chat.intent_funnel"):
        out = await funnel.route(_ctx(MSG), deps=deps, requirements=req)
    assert out is not req
    act = out.requested_action
    assert act["tool"] == "create_folder" and act["args"] == {"name": "季度报告"}
    assert act["capability_id"] == "cap-a"
    assert act["registry_version"] == "idx-9"          # executor TOCTOU namespace
    assert act["funnel_registry_version"] == view.fingerprint
    assert act["funnel_stage"] == "decision"
    assert out.needs_action is Signal.HIGH and out.complexity is Complexity.LOW
    line = next(r.getMessage() for r in caplog.records if "funnel_trace" in r.getMessage())
    assert "final_route=action" in line and "fallback_reason=-" in line
    assert "judge=UNCERTAIN" in line and "decision=cap-a" in line


async def test_matcher_mode_on_certifies_without_any_llm(monkeypatch):
    _open(monkeypatch, mode="on")
    llm = _LLM()
    _, deps = _wire(monkeypatch, view=_view(CAP), index=_index([]),
                    embedder=_Embedder([1.0, 0.0]), llm=llm)
    out = await funnel.route(_ctx(MSG), deps=deps, requirements=_req())
    assert out.requested_action["funnel_stage"] == "matcher"
    assert out.requested_action["args"] == {"name": "季度报告"}
    assert llm.prompts == []      # deterministic certification: zero model spend


async def test_recall_confident_path_skips_decision(monkeypatch):
    _open(monkeypatch)
    llm = _LLM()
    idx = _index([("cap-a", [MSG], [[1.0, 0.0]])])
    _, deps = _wire(monkeypatch, view=_view(CAP), index=idx,
                    embedder=_Embedder([1.0, 0.0]), llm=llm)
    out = await funnel.route(_ctx(MSG), deps=deps, requirements=_req())
    assert out.requested_action["funnel_stage"] == "judge"  # stub CONFIDENT
    assert llm.prompts == []


async def test_decision_none_returns_original(monkeypatch, caplog):
    _open(monkeypatch)
    llm = _LLM([{"capability_id": "NONE"}])
    # two near-tied recall candidates: stub escalates, Decision answers NONE
    idx = _index([("cap-a", ["建个目录"], [[0.9, 0.43]]),
                  ("cap-b", ["加个词"], [[0.9, 0.44]])])
    view = _view(CAP + [_entry("cap-b", tool="add_term")])
    _, deps = _wire(monkeypatch, view=view, index=idx,
                    embedder=_Embedder([1.0, 0.0]), llm=llm)
    req = _req()
    with caplog.at_level(logging.INFO, logger="core.application.chat.intent_funnel"):
        out = await funnel.route(_ctx("整理一下笔记好吗"), deps=deps, requirements=req)
    assert out is req
    line = next(r.getMessage() for r in caplog.records if "funnel_trace" in r.getMessage())
    assert f"fallback_reason={REASON_DECISION_NONE}" in line


async def test_verdict_not_in_active_table_is_version_mismatch(monkeypatch, caplog):
    _open(monkeypatch)
    idx = _index([("cap-ghost", ["x"], [[1.0, 0.0]])])   # index ahead of table
    _, deps = _wire(monkeypatch, view=_view(CAP), index=idx,
                    embedder=_Embedder([1.0, 0.0]), llm=_LLM())
    req = _req()
    with caplog.at_level(logging.INFO, logger="core.application.chat.intent_funnel"):
        out = await funnel.route(_ctx("x"), deps=deps, requirements=req)
    assert out is req
    line = next(r.getMessage() for r in caplog.records if "funnel_trace" in r.getMessage())
    assert f"fallback_reason={REASON_VERSION_MISMATCH}" in line


async def test_bind_missing_escalates_through_recheck_to_agent(monkeypatch, caplog):
    _open(monkeypatch)
    view = _view([_entry("cap-a", aliases=("新建文件夹",))])
    idx = _index([("cap-a", ["建个目录"], [[0.2, 0.8]])])
    _, deps = _wire(monkeypatch, view=view, index=idx,
                    embedder=_Embedder([1.0, 0.0]),
                    llm=_LLM([{"capability_id": "cap-a"}]))
    req = _req()
    with caplog.at_level(logging.INFO, logger="core.application.chat.intent_funnel"):
        out = await funnel.route(_ctx("新建文件夹"), deps=deps, requirements=req)
    assert out is req                       # Agent owns the clarification (8.7)
    line = next(r.getMessage() for r in caplog.records if "funnel_trace" in r.getMessage())
    assert f"fallback_reason={REASON_BIND_MISSING}" in line
    assert "judge=recheck:UNCERTAIN" in line


async def test_c2_integrity_certifies_terminal_marker(monkeypatch, caplog):
    _open(monkeypatch, mode="on")
    view = _view([_entry("cap-a", tool="ghost_tool", aliases=("召唤幽灵",))])
    _, deps = _wire(monkeypatch, view=view, index=_index([]),
                    embedder=_Embedder([1.0, 0.0]), llm=_LLM())
    req = _req()
    with caplog.at_level(logging.INFO, logger="core.application.chat.intent_funnel"):
        out = await funnel.route(_ctx("召唤幽灵"), deps=deps, requirements=req)
    assert out is not req                   # never the Agent as a recovery channel
    act = out.requested_action
    assert act["binding_integrity"] and act["args"] is None
    assert act["tool"] == "ghost_tool"
    line = next(r.getMessage() for r in caplog.records if "funnel_trace" in r.getMessage())
    assert "final_route=action" in line and "deepest_stage=certified" in line


async def test_cascade_timeout_is_attributed_to_its_stage(monkeypatch, caplog):
    _open(monkeypatch, timeout=0.05)

    async def slow_load(sf):
        await asyncio.sleep(0.3)
        return _index([])

    monkeypatch.setattr(
        "core.application.chat.intent_funnel.registry.active_view",
        lambda **kw: asyncio.ensure_future(_view_ok()),
    )
    monkeypatch.setattr("core.application.chat.intent_funnel.recall.load_index", slow_load)
    deps = types.SimpleNamespace(session_factory=None,
                                 embedder=lambda: _Embedder([1.0, 0.0]), llm=_LLM())
    req = _req()
    with caplog.at_level(logging.INFO, logger="core.application.chat.intent_funnel"):
        out = await funnel.route(_ctx(MSG), deps=deps, requirements=req)
    assert out is req
    line = next(r.getMessage() for r in caplog.records if "funnel_trace" in r.getMessage())
    assert f"fallback_reason={REASON_RECALL_TIMEOUT}" in line
    assert "deepest_stage=recall" in line


async def _view_ok():
    return _view(CAP)


async def test_negated_matcher_hit_is_forced_to_miss(monkeypatch, caplog):
    _open(monkeypatch, mode="on")
    # regex alias so "不要新建文件夹" would HIT without the negation guard
    view = _view([_entry("cap-a", patterns=("re:新建文件夹",))])
    _, deps = _wire(monkeypatch, view=view, index=_index([]),
                    embedder=_Embedder([1.0, 0.0]), llm=_LLM())
    req = _req()
    with caplog.at_level(logging.INFO, logger="core.application.chat.intent_funnel"):
        out = await funnel.route(_ctx("不要新建文件夹"), deps=deps, requirements=req)
    assert out is req
    line = next(r.getMessage() for r in caplog.records if "funnel_trace" in r.getMessage())
    assert "matcher=MISS" in line and f"fallback_reason={REASON_NO_CANDIDATE}" in line


async def test_ambiguous_carries_all_candidates_upward(monkeypatch):
    _open(monkeypatch)
    v = _view([_entry("cap-a", aliases=("季度汇总",)),
               _entry("cap-b", tool="add_term", aliases=("季度汇总",))])
    llm = _LLM([{"capability_id": "cap-b", "rationale": "term wins"}])
    _, deps = _wire(monkeypatch, view=v, index=_index([]),
                    embedder=_Embedder([1.0, 0.0]), llm=llm)
    # decision picks cap-b but its binder can not certify this sentence -> C1 exit
    req = _req()
    out = await funnel.route(_ctx("季度汇总"), deps=deps, requirements=req)
    assert out is req
    assert "cap-a" in llm.prompts[0] and "cap-b" in llm.prompts[0]  # BOTH carried up
