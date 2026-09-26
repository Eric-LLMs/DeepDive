"""P2 — node-level tests + the single-hop cascade through funnel.route.

Chain ruling 2026-09-24: Matcher HIT -> ToolIntentModel (ONE call: select+extract);
MISS/AMBIGUOUS -> Recall -> same ToolIntentModel; Binder validates the draft; every
failure exits to the Agent. No recheck hop, no Decision node in the active path.

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
    MATCH_HIT,
    REASON_BIND_MISSING,
    REASON_NO_CANDIDATE,
    REASON_RECALL_TIMEOUT,
    REASON_RECALL_UNAVAILABLE,
    REASON_REGISTRY_UNAVAILABLE,
    REASON_TOOL_INTENT_REJECT,
    REASON_TOOL_INTENT_UNCERTAIN,
    REASON_VERSION_MISMATCH,
    TOOL_INTENT_CONFIDENT,
    TOOL_INTENT_REJECT,
    TOOL_INTENT_UNCERTAIN,
    Candidate,
    TurnFacts,
)
from core.application.chat.intent_funnel.registry import content_fingerprint
from core.application.chat.intent_funnel.registry import entry as T
from core.application.chat.intent_funnel.tool_intent import base as jbase
from core.application.chat.understanding import (
    Complexity,
    Confidence,
    Signal,
    TurnRequirements,
)

# ════════════════════════ shared fakes (contract-shaped, minimal) ═══════════════


def _q(i, text, *, position=0, standard_query_id=None, kind="standard"):
    return T.QueryRecord(id=f"q{i}", query=text,
                         language=T.derive_language(text), position=position,
                         standard_query_id=standard_query_id)


def _entry(cid, *, tool="create_folder", corpus=(), patterns=(), aliases=(),
           arg_slots=None, enabled=True, status="active", examples=("做个事",),
           negatives=(), parameters=None):
    # ``corpus`` feeds the EXACT set: the live-table ruling hydrates it as
    # Standard (first row) + Similar query rows (ruling 2026-09-25/26);
    # patterns/aliases stay storable but are inert as far as the Matcher goes.
    corpus = tuple(corpus)
    sims = tuple(_q(10 + n, s, position=n, standard_query_id="q1" if corpus else None)
                 for n, s in enumerate(corpus[1:]))
    return T.CapabilityEntry(
        capability_id=cid, tool_binding=tool, description=f"does {cid}",
        standard_queries=(_q(1, corpus[0]),) if corpus else (),
        similar_queries=sims,
        patterns=tuple(patterns), aliases=tuple(aliases),
        request_query_examples=tuple(examples),
        negatives=tuple(negatives), arg_slots=arg_slots if arg_slots is not None
        else {"name": {"source": "user_input"}},
        parameters=dict(parameters) if parameters is not None else {},
        enabled=enabled, status=status,
    )


# canonical-shaped schemas for the seeded tools (ToolIntentModel argument targets)
_NAME_SCHEMA = {"name": {"type": "string", "description": "folder name",
                         "required": True, "max_len": 120}}
_TERM_SCHEMA = {
    "term": {"type": "string", "description": "the term", "required": True, "max_len": 120},
    "domain": {"type": "string", "description": "vocabulary domain",
               "required": True, "max_len": 60},
}


def _view(entries, version=1):
    # live-table ruling 2026-09-26: the read model IS the live corpus, keyed
    # only by its content fingerprint (the ``version`` arg is call-site noise).
    return T.RegistryLiveView(
        fingerprint=content_fingerprint(list(entries)),
        entries=tuple(entries),
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
    temperature) so the online model's explicit forwarding is assertable."""

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


def _index(caps, *, version="corpus1-test"):
    """caps: [(capability_id, [sentences], [sentence vectors])]. Mirrors the
    LIVE corpus rows the real recall's in-process lane consumes (no
    session_factory on the fake -> no pgvector ANN in unit tests)."""
    corpus = []
    for cid, examples, vecs in caps:
        for i, (q, v) in enumerate(zip(examples, vecs)):
            corpus.append(types.SimpleNamespace(
                kind="standard", query_id=f"{cid}-{i}", capability_id=cid,
                query=q, language=T.derive_language(q),
                standard_query_id=None, vector=list(v),
            ))
    return types.SimpleNamespace(version=version, corpus=tuple(corpus))


def _ctx(msg, **kw):
    base = {
        "body": types.SimpleNamespace(message=msg, attach=None, viewer=None),
        "owned_asset_id": None, "research_turn": False, "effective_handoff": None,
        "session_id": "s-1",
    }
    base.update(kw)
    return types.SimpleNamespace(**base)


def _req(**kw):
    base = {"complexity": Complexity.LOW, "confidence": Confidence.LOW,
                "needs_web": Signal.LOW, "needs_memory": False}
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
    v = _view([_entry("cap-a", corpus=("新建文件夹",))])
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


async def test_recall_keeps_every_hit_with_provenance_and_never_adjudicates():
    from core.application.chat.intent_funnel import recall as recall_node

    idx = _index([
        ("cap-a", ["做a1", "做a2"], [[1.0, 0.0], [1.0, 0.1]]),  # BOTH kept
        ("cap-b", ["加个词"], [[1.0, 0.5]]),
        ("cap-c", ["闲聊"], [[1.0, 2.0]]),                      # below min_score
    ])
    emb = _Embedder([1.0, 0.0])
    res = await recall_node.recall(idx, "新建文件夹", embedder=emb, min_score=0.82)
    ids = [c.capability_id for c in res.candidates]
    scores = [c.score for c in res.candidates]
    assert scores == sorted(scores, reverse=True)  # ranked, NOT margin-aborted
    assert ids.count("cap-a") == 2                 # no per-capability MAX merge:
    assert "cap-c" not in ids                      # every hit >= gate is its own candidate
    top = res.candidates[0]
    assert top.matched_example == "做a1" and top.origin == "recall"
    assert top.query_kind == "standard" and top.language == "zh" and top.query_id
    assert emb.calls == 1                          # ONE embedding for BOTH paths


async def test_recall_blank_query_and_embedder_fault():
    from core.application.chat.intent_funnel import recall as recall_node

    idx = _index([("cap-a", ["做a"], [[1.0, 0.0]])])
    assert (await recall_node.recall(idx, "  ", embedder=_Embedder([1.0]),
                                     min_score=0.5)).candidates == ()
    with pytest.raises(RuntimeError):
        await recall_node.recall(idx, "做a", embedder=_Embedder([1.0], fail=True),
                                 min_score=0.5)
    with pytest.raises(RuntimeError):
        await recall_node.recall(idx, "做a", embedder=_Embedder([]),
                                 min_score=0.5)


async def test_recall_gate_is_inclusive_near_ties_survive():
    from core.application.chat.intent_funnel import recall as recall_node

    # recall proposes, the ToolIntentModel disposes (design §3): the gate is a
    # >= filter, nothing more — no margin cut, no winner pick.
    idx = _index([("cap-a", ["a"], [[1.0, 0.0]]),
                  ("cap-b", ["b"], [[1.0, 0.02]])])
    res = await recall_node.recall(idx, "q", embedder=_Embedder([1.0, 0.0]),
                                   min_score=1.0)
    assert [c.capability_id for c in res.candidates] == ["cap-a"]  # exact 1.0 kept


# ═══════════════════════════════ tool_intent: stub + ladder ═══════════════════════════


def test_stub_three_states():
    from core.application.chat.intent_funnel.tool_intent import stub

    assert stub.evaluate((), margin=0.06).decision == TOOL_INTENT_REJECT
    one = stub.evaluate((Candidate("cap-a", 0.91),), margin=0.06)
    assert one.decision == TOOL_INTENT_CONFIDENT and one.capability_id == "cap-a"
    m = stub.evaluate((Candidate("cap-a", 0.0, origin="matcher_ambiguous"),), margin=0.06)
    assert m.decision == TOOL_INTENT_UNCERTAIN          # uncalibrated score: no cert
    far = stub.evaluate((Candidate("cap-a", 0.90), Candidate("cap-b", 0.70)), margin=0.06)
    assert far.decision == TOOL_INTENT_CONFIDENT and far.capability_id == "cap-a"
    near = stub.evaluate((Candidate("cap-a", 0.90), Candidate("cap-b", 0.88)), margin=0.06)
    assert near.decision == TOOL_INTENT_UNCERTAIN       # margin below floor: escalate up


async def test_tool_intent_ladder_falls_through_and_sanitizes(monkeypatch):
    from core.application.chat.intent_funnel import tool_intent as ti_pkg
    from core.config import settings

    monkeypatch.setattr(settings, "chat_tool_intent_backend", "local")  # not deployed
    monkeypatch.setattr(settings, "chat_tool_intent_local_url", "")

    cands = (Candidate("cap-a", 0.9),)
    out = await ti_pkg.select_and_extract("q", cands, entries_by_id={}, llm=None)
    # local unavailable -> fall through (NOT abstain); no llm -> online also down
    assert out.decision == TOOL_INTENT_UNCERTAIN

    monkeypatch.setattr(settings, "chat_tool_intent_backend", "SHTUB")  # typo
    out = await ti_pkg.select_and_extract("q", cands, entries_by_id={}, llm=None)
    assert out.decision == TOOL_INTENT_CONFIDENT  # unknown backend -> stub, single recall


async def test_tool_intent_reply_discipline(monkeypatch):
    from core.application.chat.intent_funnel import tool_intent as ti_pkg
    from core.config import settings

    monkeypatch.setattr(settings, "chat_tool_intent_min_confidence", 0.75)
    cands = (Candidate("cap-a", 0.9), Candidate("cap-b", 0.5))

    v = ti_pkg._verdict_from_reply({"capability_id": "NONE"}, cands)
    assert v.decision == TOOL_INTENT_REJECT and v.arguments is None
    v = ti_pkg._verdict_from_reply({"capability_id": "cap-z", "confidence": 1.0}, cands)
    assert v.decision == TOOL_INTENT_UNCERTAIN              # off-card is never a verdict
    v = ti_pkg._verdict_from_reply({"capability_id": "cap-a", "confidence": 0.4}, cands)
    assert v.decision == TOOL_INTENT_UNCERTAIN              # under the floor
    v = ti_pkg._verdict_from_reply(
        {"capability_id": "cap-a", "confidence": 0.9, "arguments": {"name": "x"}}, cands)
    assert v.decision == TOOL_INTENT_CONFIDENT and v.capability_id == "cap-a"
    assert v.arguments == {"name": "x"}               # ToolIntentModel's draft rides along
    # a non-dict arguments field is dirty data, never a partial answer
    v = ti_pkg._verdict_from_reply(
        {"capability_id": "cap-a", "confidence": 0.9, "arguments": "name=x"}, cands)
    assert v.decision == TOOL_INTENT_CONFIDENT and v.arguments is None


def test_tool_intent_backends_raise_unavailable_not_answers():
    from core.application.chat.intent_funnel.tool_intent import local, online

    async def go():
        with pytest.raises(jbase.ToolIntentUnavailable):
            await local.model_reply("q", (), {}, url="")
        with pytest.raises(jbase.ToolIntentUnavailable):
            await online.model_reply("q", (), {}, llm=None)
        with pytest.raises(jbase.ToolIntentUnavailable):
            await online.model_reply("q", (), {}, llm=_LLM([RuntimeError("401")]))
    asyncio.run(go())


# ── 8.17 real-backend wiring (2026-09-24 ruling: online first, dedicated
#    small-model channel with explicit per-call forwarding) ──────────────────────

async def test_tool_intent_online_serves_and_forwards_dedicated_channel(monkeypatch):
    from core.application.chat.intent_funnel import tool_intent as ti_pkg
    from core.config import settings

    monkeypatch.setattr(settings, "chat_tool_intent_backend", "online")
    monkeypatch.setattr(settings, "chat_tool_intent_online_model", "tiny-model")
    monkeypatch.setattr(settings, "chat_tool_intent_online_base_url", "https://cheap.example/v1")
    monkeypatch.setattr(settings, "chat_tool_intent_online_api_key", "sk-test")
    llm = _LLM([{"capability_id": "cap-a", "confidence": 0.9, "arguments": {"name": "n"}}])
    out = await ti_pkg.select_and_extract(
        "新建文件夹", (Candidate("cap-a", 0.9),), entries_by_id={}, llm=llm)
    assert out.decision == TOOL_INTENT_CONFIDENT and out.capability_id == "cap-a"
    assert out.arguments == {"name": "n"}
    kw = llm.calls[0]
    assert kw == {
        "model": "tiny-model", "base_url": "https://cheap.example/v1",
        "api_key": "sk-test", "timeout": settings.chat_tool_intent_timeout_seconds,
        "temperature": 0.0,
        # ToolIntentModel pins (per-call at the model call site, not the global knob): reasoning
        # explicitly off + output bound generous enough for the argument draft.
        "max_tokens": 256, "disable_thinking": True,
    }


async def test_tool_intent_online_model_without_endpoint_pair_rides_pinned_channel(monkeypatch):
    """base_url/api_key are honored only as a PAIR: a half-configured dedicated
    endpoint is worse than riding the turn's pinned channel, so only the model
    name is forwarded."""
    from core.application.chat.intent_funnel import tool_intent as ti_pkg
    from core.config import settings

    monkeypatch.setattr(settings, "chat_tool_intent_backend", "online")
    monkeypatch.setattr(settings, "chat_tool_intent_online_model", "tiny-model")
    monkeypatch.setattr(settings, "chat_tool_intent_online_base_url", "https://cheap.example/v1")
    monkeypatch.setattr(settings, "chat_tool_intent_online_api_key", "")
    llm = _LLM([{"capability_id": "cap-a", "confidence": 0.9}])
    out = await ti_pkg.select_and_extract(
        "q", (Candidate("cap-a", 0.9),), entries_by_id={}, llm=llm)
    assert out.decision == TOOL_INTENT_CONFIDENT
    assert llm.calls[0].get("model") == "tiny-model"
    assert "base_url" not in llm.calls[0] and "api_key" not in llm.calls[0]


async def test_tool_intent_auto_local_absent_falls_through_to_online(monkeypatch):
    """8.17 ladder: auto with nothing deployed = local skipped (fall-through,
    never abstain-to-Agent) and the online step really serves."""
    from core.application.chat.intent_funnel import tool_intent as ti_pkg
    from core.config import settings

    monkeypatch.setattr(settings, "chat_tool_intent_backend", "auto")
    monkeypatch.setattr(settings, "chat_tool_intent_local_url", "")
    llm = _LLM([{"capability_id": "cap-a", "confidence": 0.95}])
    out = await ti_pkg.select_and_extract(
        "q", (Candidate("cap-a", 0.9),), entries_by_id={}, llm=llm)
    assert out.decision == TOOL_INTENT_CONFIDENT and llm.prompts


async def test_tool_intent_auto_full_chain_local_unreachable_online_down_stub_serves(monkeypatch):
    from core.application.chat.intent_funnel import tool_intent as ti_pkg
    from core.config import settings

    monkeypatch.setattr(settings, "chat_tool_intent_backend", "auto")
    # a dead port: transport fault -> ToolIntentUnavailable -> fall through
    monkeypatch.setattr(settings, "chat_tool_intent_local_url", "http://127.0.0.1:9/v1")
    out = await ti_pkg.select_and_extract(
        "q", (Candidate("cap-a", 0.9),), entries_by_id={}, llm=None)
    # local down + online (no llm) down -> the deterministic stub serves
    assert out.decision == TOOL_INTENT_CONFIDENT and out.capability_id == "cap-a"
    # the stub has NO extraction power: the draft stays None (honest, documented)
    assert out.arguments is None


def test_local_model_speaks_openai_wire_or_raises_unavailable(monkeypatch):
    import httpx
    from core.application.chat.intent_funnel.tool_intent import local as local_mod

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
        "content": 'verdict: {"capability_id": "cap-a", "confidence": 0.88, '
                   '"arguments": {"name": "报告"}}'}}]}

    async def go():
        monkeypatch.setattr(local_mod.httpx, "AsyncClient",
                            lambda **kw: _Client(_Resp(200, ok_body)))
        data = await local_mod.model_reply("q", (), {}, url="http://j/v1/")
        assert data == {"capability_id": "cap-a", "confidence": 0.88,
                        "arguments": {"name": "报告"}}
        assert seen["url"] == "http://j/v1/chat/completions"     # base + wire
        assert seen["payload"]["messages"][0]["role"] == "system"
        assert seen["payload"]["temperature"] == 0.0
        # provider config: the DEPLOYED default is Qwen3-0.6B at Q4_K_M (the
        # quantization rides the Ollama tag); it appears in config/compose ONLY.
        from core.config import settings
        assert settings.chat_tool_intent_local_model == "qwen3:0.6b-q4_K_M"
        assert seen["payload"]["model"] == "qwen3:0.6b-q4_K_M"
        # an explicit "" falls back to the server's own default model
        monkeypatch.setattr(settings, "chat_tool_intent_local_model", "")
        await local_mod.model_reply("q", (), {}, url="http://j/v1")
        assert "model" not in seen["payload"]
        # provider swap: the model NAME comes from config only (compose sets both)
        monkeypatch.setattr(settings, "chat_tool_intent_local_model", "qwen-test:0.6b")
        await local_mod.model_reply("q", (), {}, url="http://j/v1")
        assert seen["payload"]["model"] == "qwen-test:0.6b"
        monkeypatch.setattr(local_mod.httpx, "AsyncClient",
                            lambda **kw: _Client(_Resp(503, {})))
        with pytest.raises(jbase.ToolIntentUnavailable):
            await local_mod.model_reply("q", (), {}, url="http://j/v1")
        # a 200 whose message is not JSON is UNAVAILABLE, never a verdict
        monkeypatch.setattr(local_mod.httpx, "AsyncClient", lambda **kw: _Client(
            _Resp(200, {"choices": [{"message": {"content": "no json here"}}]})))
        with pytest.raises(jbase.ToolIntentUnavailable):
            await local_mod.model_reply("q", (), {}, url="http://j/v1")
    asyncio.run(go())


# ═══════════════════ ToolIntentModel local provider: native tool-calling mode ═══════════════════


def test_local_tools_mode_sends_registry_tools_and_reads_tool_call(monkeypatch):
    """mode=tools (Phase-1 ruling): each candidate is sent as an OpenAI function
    (name = Registry capability_id, params = Registry schema); the reply is read
    back from message.tool_calls into the SAME {capability_id, confidence,
    arguments} shape. Off-card names and refusals still fail closed downstream."""
    import httpx
    from core.application.chat.intent_funnel import tool_intent as ti_pkg
    from core.application.chat.intent_funnel.tool_intent import local as local_mod
    from core.config import settings

    seen: dict = {}

    class _Resp:
        def __init__(self, status, body):
            self.status_code, self._body = status, body

        def raise_for_status(self):
            if self.status_code >= 400:
                raise httpx.HTTPStatusError("boom",
                    request=httpx.Request("POST", "http://j/v1/chat/completions"),
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

    entry = _entry("cap-a", parameters=_NAME_SCHEMA)
    cands = (Candidate("cap-a", 1.0, origin="matcher_hit"),)
    monkeypatch.setattr(settings, "chat_tool_intent_local_mode", "tools")

    def tool_body(name, args):
        return {"choices": [{"message": {
            "role": "assistant", "content": "",
            "tool_calls": [{"id": "c1", "type": "function",
                            "function": {"name": name, "arguments": args}}]}}]}

    async def go():
        # a valid on-card tool call -> selection + extracted draft, provider conf 1.0
        monkeypatch.setattr(local_mod.httpx, "AsyncClient",
                            lambda **kw: _Client(_Resp(200, tool_body("cap-a", '{"name": "报告"}'))))
        data = await local_mod.model_reply("新建文件夹", cands, {"cap-a": entry}, url="http://j/v1")
        assert data == {"capability_id": "cap-a", "confidence": 1.0, "arguments": {"name": "报告"}}
        # the tool definitions came from the Registry (name=cap id, params from schema)
        tool = seen["payload"]["tools"][0]["function"]
        assert tool["name"] == "cap-a"
        assert tool["parameters"]["properties"]["name"]["type"] == "string"
        assert "name" in tool["parameters"]["required"]
        assert seen["payload"]["tool_choice"] == "auto"     # abstention allowed
        assert seen["payload"]["reasoning_effort"] == "none"  # provider pin kept
        assert seen["payload"]["temperature"] == 0.0        # identical generation
        # the verdict node still certifies it (candidate membership -> CONFIDENT)
        v = ti_pkg._verdict_from_reply(data, cands)
        assert v.decision == TOOL_INTENT_CONFIDENT and v.arguments == {"name": "报告"}

        # NO tool-call -> a legitimate refusal (NONE) -> REJECT, never executed
        monkeypatch.setattr(local_mod.httpx, "AsyncClient", lambda **kw: _Client(
            _Resp(200, {"choices": [{"message": {"role": "assistant",
                     "content": "no tool fits here", "tool_calls": []}}]})))
        data = await local_mod.model_reply("今天天气", cands, {"cap-a": entry}, url="http://j/v1")
        assert data["capability_id"] == "NONE" and data["arguments"] is None
        assert ti_pkg._verdict_from_reply(data, cands).decision == TOOL_INTENT_REJECT

        # off-card function name with provider conf 1.0 -> STILL UNCERTAIN:
        # the confidence=1.0 never bypasses capability correctness (candidate check first)
        monkeypatch.setattr(local_mod.httpx, "AsyncClient", lambda **kw: _Client(
            _Resp(200, tool_body("some-invented-tool", "{}"))))
        data = await local_mod.model_reply("q", cands, {"cap-a": entry}, url="http://j/v1")
        assert data["capability_id"] == "some-invented-tool"
        assert ti_pkg._verdict_from_reply(data, cands).decision == TOOL_INTENT_UNCERTAIN

        # tool-call whose arguments are not valid JSON -> UNAVAILABLE (never a verdict)
        monkeypatch.setattr(local_mod.httpx, "AsyncClient", lambda **kw: _Client(
            _Resp(200, tool_body("cap-a", "{not json"))))
        with pytest.raises(jbase.ToolIntentUnavailable):
            await local_mod.model_reply("q", cands, {"cap-a": entry}, url="http://j/v1")

        # a 5xx from the model service is UNAVAILABLE (fall through), not a verdict
        monkeypatch.setattr(local_mod.httpx, "AsyncClient", lambda **kw: _Client(_Resp(503, {})))
        with pytest.raises(jbase.ToolIntentUnavailable):
            await local_mod.model_reply("q", cands, {"cap-a": entry}, url="http://j/v1")
    asyncio.run(go())
    monkeypatch.setattr(settings, "chat_tool_intent_local_mode", "prompt_json")


def test_local_tools_markdown_fallback_normalizes_to_same_internal_shape(monkeypatch):
    """The structured-Markdown tool output the CPU checkpoint emits when the
    native tool-call token loses the first-token argmax is a WIRE-FORMAT issue,
    not a semantic miss: the model already chose the right capability/tool/
    args/confidence. When there is NO native tool_calls, the reader normalizes
    the strict Markdown block into the SAME {capability_id, confidence,
    arguments} shape the native path yields, so it enters the existing
    Binder -> Runtime chain unchanged (no re-ask, no Agent hand-off).

    The parser is deliberately strict — only a whole-reply, structurally valid
    block with a JSON-object arguments AND a capability/tool consistent with
    the Candidate/Registry counts. Prose, malformed blocks, off-card ids, bad
    JSON and schema mismatches must all stay out of the ToolIntent path."""
    from core.application.chat.intent_funnel import tool_intent as ti_pkg
    from core.application.chat.intent_funnel.binder import validate as bind_validate
    from core.application.chat.intent_funnel.tool_intent import local as local_mod
    from core.config import settings

    class _Resp:
        def __init__(self, body):
            self._body = body

        def raise_for_status(self):
            return None

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
            return self._resp

    entry = _entry("cap-a", parameters=_NAME_SCHEMA)            # tool_binding=create_folder
    cands = (Candidate("cap-a", 1.0, origin="matcher_hit"),)
    monkeypatch.setattr(settings, "chat_tool_intent_local_mode", "tools")

    def content_body(text):
        return {"choices": [{"message": {"role": "assistant", "content": text,
                                         "tool_calls": []}}]}

    def run(text):
        async def go():
            monkeypatch.setattr(local_mod.httpx, "AsyncClient",
                                lambda **kw: _Client(_Resp(content_body(text))))
            data = await local_mod.model_reply("新建文件夹", cands, {"cap-a": entry},
                                               url="http://j/v1")
            return data, ti_pkg._verdict_from_reply(data, cands)
        return asyncio.run(go())

    # 1) native tool_calls -> unchanged: the reader path is identical and is
    #    CERTIFIED CONFIDENT, then the Binder validates the draft.
    native = {"choices": [{"message": {"role": "assistant", "content": "",
                      "tool_calls": [{"id": "c1", "type": "function",
                                      "function": {"name": "cap-a",
                                                   "arguments": '{"name":"报告"}'}}]}}]}

    async def native_go():
        monkeypatch.setattr(local_mod.httpx, "AsyncClient",
                            lambda **kw: _Client(_Resp(native)))
        return await local_mod.model_reply("q", cands, {"cap-a": entry}, url="http://j/v1")
    nd = asyncio.run(native_go())
    assert nd == {"capability_id": "cap-a", "confidence": 1.0, "arguments": {"name": "报告"}}
    nv = ti_pkg._verdict_from_reply(nd, cands)
    assert nv.decision == TOOL_INTENT_CONFIDENT
    assert bind_validate(entry, nv.arguments).state == BIND_COMPLETE

    # 2) strict Markdown block -> SAME internal shape -> same gate -> same bind
    data, v = run('### cap-a\ntool: create_folder\narguments: {"name": "季度报告"}\nconfidence: 1.000')
    assert data == {"capability_id": "cap-a", "confidence": 1.0, "arguments": {"name": "季度报告"}}
    assert v.decision == TOOL_INTENT_CONFIDENT and v.arguments == {"name": "季度报告"}
    assert bind_validate(entry, v.arguments).state == BIND_COMPLETE

    # 2b) the optional ``tool:`` line may be omitted (hit-folder-2 variant)
    data, v = run('### cap-a\narguments: {"name": "临时草稿"}\nconfidence: 0.9')
    assert v.decision == TOOL_INTENT_CONFIDENT and v.arguments == {"name": "临时草稿"}

    # 3) malformed Markdown -> NOT a tool intent -> REJECT (prose cannot bind)
    for bad in (
        "### cap-a\ntool: create_folder\narguments: {not json}\nconfidence: 1.0",   # bad JSON
        '### cap-a\narguments: "name"\nconfidence: 1.0',                             # args not object
        '### cap-a\narguments: {"name": "x"}\nconfidence: high',                     # bad confidence
        '### cap-a\narguments: {"name": "x"}',                                       # no confidence line
        'cap-a: {"name": "x"} confidence: 1.0',                                      # not a heading block
        ('### cap-a\ntool: create_folder\narguments: {"name": "x"}\nconfidence: 1.0\n'
         'more trailing prose here'),                                                # leading/trailing prose
        '',                                                                          # empty
        'NONE, the user sentence is a greeting, not a request.',                     # plain refusal prose
        'The user wants a folder. capability_id: cap-a',                             # prose that mentions a cap
    ):
        data, v = run(bad)
        assert v.decision in (TOOL_INTENT_REJECT, TOOL_INTENT_UNCERTAIN), bad
        assert data["capability_id"] in ("NONE",) or v.capability_id is None, bad

    # 4) capability NOT on the card / not in the Registry -> never an auto-pass
    #    (an off-card id WITHOUT a tool line stays the gate's UNCERTAIN; a
    #    Markdown whose tool line cannot be matched against the Registry is
    #    disqualified by the parser itself -> REJECT)
    data, v = run('### cap-invented\narguments: {"name": "x"}\nconfidence: 1.0')
    assert v.decision == TOOL_INTENT_UNCERTAIN            # off-card id, gate first
    data, v = run('### cap-invented\ntool: create_folder\narguments: {"name": "x"}\nconfidence: 1.0')
    assert v.decision == TOOL_INTENT_REJECT               # cap not in Registry at all
    data, v = run('### cap-a\ntool: wrong_binding\narguments: {"name": "x"}\nconfidence: 1.0')
    assert v.decision == TOOL_INTENT_REJECT               # tool line contradicts Registry

    # 5) arguments pass the parser but FAIL the Binder schema -> the Binder, not
    #    the parser, owns that rejection: still no bindable ToolIntent to Runtime
    data, v = run('### cap-a\narguments: {"unexpected_slot": "x"}\nconfidence: 1.0')
    assert v.decision == TOOL_INTENT_CONFIDENT            # parser only normalizes
    assert bind_validate(entry, v.arguments).state == BIND_INVALID  # Binder stops it
    monkeypatch.setattr(settings, "chat_tool_intent_local_mode", "prompt_json")


def test_local_mode_default_is_prompt_json_and_unchanged():
    """The Phase-1 addition must not move today's behavior: with no explicit mode
    the adapter speaks the original card JSON contract."""
    from core.config import settings
    assert settings.chat_tool_intent_local_mode == "prompt_json"
    from core.application.chat.intent_funnel.tool_intent import local as local_mod
    assert local_mod.MODES == ("prompt_json", "tools")


# ═══════════════════════ ToolIntentModel prompt: full Candidate Card assembly ═══════════


def test_tool_intent_card_carries_tool_schema_score_and_origin():
    entry = _entry("cap-a", examples=("建个目录",), parameters=_NAME_SCHEMA)
    cands = (Candidate("cap-a", 0.676, matched_example="建个目录", origin="recall"),)
    facts = TurnFacts(has_attachment=True, viewer_asset_id="a-7")
    p = jbase.build_prompt("新建文件夹", cands, {"cap-a": entry}, facts=facts)
    # the Card fields the chain ruling requires, verbatim
    assert "### cap-a" in p
    assert "tool: create_folder" in p                 # tool/function binding
    assert "does: does cap-a" in p                    # tool description
    assert "matched_example: 建个目录" in p            # which corpus sentence hit
    assert "origin=recall score=0.676" in p           # recall score + candidate origin
    assert "- name (string, required, max_len=120): folder name" in p  # param schema+desc
    assert "has_attachment=1" in p and "viewer_asset_id=a-7" in p      # TurnFacts line
    assert "<user_sentence>新建文件夹</user_sentence>" in p             # data, not instructions
    # an empty schema says so explicitly — no invented slots
    p2 = jbase.build_prompt("q", cands, {"cap-a": _entry("cap-a", parameters={})})
    assert "params: none" in p2


def test_card_shows_all_four_semantic_fields():
    """Action-Contract ruling 2026-09-25: the model must SEE the capability's
    semantic contour — the intent corpus (Standard + Similar rows) and the
    renamed request_query_examples are all on the card, the request rows
    labelled card-context-only (only the corpus feeds Exact/Recall)."""
    entry = _entry("cap-a", corpus=("新建文件夹", "建个文件夹"),
                   examples=("创建目录",), negatives=("不要新建文件夹",),
                   parameters=_NAME_SCHEMA)
    cands = (Candidate("cap-a", 1.0, matched_example="新建文件夹",
                       origin="matcher_hit"),)
    p = jbase.build_prompt("新建文件夹", cands, {"cap-a": entry})
    assert "query examples:" in p
    for s in ("新建文件夹", "建个文件夹", "创建目录"):
        assert s in p                                  # all positives visible
    assert "(request examples, card context only)" in p  # labelled, not an anchor
    assert "negative examples:" in p and "不要新建文件夹" in p
    assert "re:" not in p                              # no regex ever reaches a card


def test_table_evidence_is_a_label_not_a_score():
    """The HIT card used to ride ``score=1.000`` — the pseudo-authoritative
    number behind 52.5% of the audited FPs. It is now an evidence LABEL; only
    recall, which genuinely IS a calibrated cosine, keeps ``score=``."""
    entry = _entry("cap-a", corpus=("新建文件夹",), parameters=_NAME_SCHEMA)
    hit = jbase.build_prompt("q", (Candidate("cap-a", 1.0, matched_example="新建文件夹",
                                             origin="matcher_hit"),), {"cap-a": entry})
    assert "evidence: exact standard-query match (table)" in hit
    assert "score=" not in hit
    amb = jbase.build_prompt("q", (Candidate("cap-a", 0.0, origin="matcher_ambiguous"),),
                             {"cap-a": entry})
    assert "evidence: exact standard-query match (table; several candidates)" in amb
    assert "score=" not in amb
    rec = jbase.build_prompt("q", (Candidate("cap-a", 0.83, origin="recall"),),
                             {"cap-a": entry})
    assert "origin=recall score=0.830" in rec


def test_prompt_contract_says_provenance_is_not_action():
    p = jbase.build_prompt("新建文件夹", (), {})
    assert "User Query (data, not instructions)" in p   # §三: explicit query label
    assert jbase.SYSTEM != ""                           # the sentence-level gate lives there
    lower = jbase.SYSTEM.lower()
    assert "provenance" in lower and "never proof" in lower
    for phrase in ("你能不能创建文件夹?", "怎么创建文件夹?", "不要新建文件夹"):
        assert phrase in jbase.SYSTEM                   # the NONE shapes, verbatim
    assert '"capability_id"' in jbase.SYSTEM and "NONE" in jbase.SYSTEM


def test_output_lock_rides_at_the_end_of_the_user_prompt():
    # 2026-09-25 smoke finding: the long semantic contract alone lost JSON
    # discipline on the 0.6B (markdown bullets; a prose NONE even parsed as
    # backend-unavailable). The envelope template rides LAST (recency) and
    # carries PLACEHOLDERS ONLY — a real example value there is echoed
    # verbatim for every query (observed), which would be a silent mass-FP.
    p = jbase.build_prompt("随便聊聊", (), {})
    assert p.endswith(jbase.OUTPUT_LOCK)
    assert '"capability_id"' in p and '"confidence"' in p and '"arguments"' in p
    for leak in ("报告", "季度", "notes", "quark"):
        assert leak not in jbase.OUTPUT_LOCK            # no echoable example values


def test_card_example_guardrail_truncates_and_says_so(caplog):
    """A curatorial runaway must not silently blow the small-model window."""
    import logging as _logging

    entry = _entry("cap-a", corpus=tuple(f"句{i}" for i in range(12)),
                   negatives=tuple(f"负{i}" for i in range(9)))
    with caplog.at_level(_logging.WARNING,
                         logger="core.application.chat.intent_funnel.tool_intent.base"):
        p = jbase.build_prompt("q", (Candidate("cap-a", 0.9, origin="recall"),),
                               {"cap-a": entry})
    assert "句11" not in p                                  # positives capped at 8
    assert "负8" not in p                                  # negatives capped at 6
    msgs = [r.getMessage() for r in caplog.records]
    assert any("truncated" in m for m in msgs)


def test_prompt_defensively_replaces_leaked_regex_literal(caplog):
    # Defense in depth (ruling 2026-09-25): the exact-only Matcher can no
    # longer hand a raw regex to a card; IF one ever leaks through a legacy
    # path, build_prompt swaps in the standard sentence and says so loudly.
    import logging as _logging

    entry = _entry("cap-a", corpus=("新建一个文件夹",))
    cands = (Candidate("cap-a", 1.0, matched_example="re:新建文件夹",
                       origin="matcher_hit"),)
    with caplog.at_level(_logging.WARNING,
                         logger="core.application.chat.intent_funnel.tool_intent.base"):
        p = jbase.build_prompt("q", cands, {"cap-a": entry})
    assert "re:新建文件夹" not in p
    assert "matched_example: 新建一个文件夹" in p
    assert any("regex literal leaked" in r.getMessage() for r in caplog.records)


def test_prompt_empty_candidate_set_is_explicit_not_silent():
    # defensive reachability: since ruling 2026-09-26 the funnel short-circuits
    # an empty set BEFORE the hop, so build_prompt only sees () if a caller
    # bypasses that guard — the prompt must still say so honestly, never render
    # an empty section.
    p = jbase.build_prompt("随便聊聊", (), {})
    assert "Candidates:\n\n(none registered for this turn)" in p
    assert "<user_sentence>随便聊聊</user_sentence>" in p


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


# ── validate-only gate over ToolIntentModel's draft (chain ruling 2026-09-24) ────────────


def test_binder_validate_normalizes_against_registry_schema():
    from core.application.chat.intent_funnel import binder

    entry = _entry("cap-a", parameters=_NAME_SCHEMA)
    ok = binder.validate(entry, {"name": "  季度报告  "})
    assert ok.state == BIND_COMPLETE and ok.args == {"name": "季度报告"}   # stripped
    assert binder.validate(entry, {"name": "x" * 121}).state == BIND_INVALID  # max_len
    assert binder.validate(entry, None).state == BIND_MISSING             # no draft
    assert binder.validate(entry, {"name": "  "}).state == BIND_MISSING   # blank required
    assert binder.validate(entry, {"bogus": "x"}).state == BIND_INVALID   # whitelist
    assert binder.validate(entry, {"name": 7}).args == {"name": "7"}      # coerced
    # non-string schema values are normalized, over-length still refused
    assert binder.validate(entry, {"name": "名" * 121}).state == BIND_INVALID
    # a schema-less capability needs no arguments: even None draft completes
    empty = _entry("cap-a", parameters={})
    done = binder.validate(empty, None)
    assert done.state == BIND_COMPLETE and done.args == {}
    # optional slot may be omitted; required ones may not
    opt = _entry("cap-a", parameters={
        "name": {"type": "string", "description": "n", "required": True},
        "note": {"type": "string", "description": "n", "required": False},
    })
    assert binder.validate(opt, {"name": "x"}).args == {"name": "x"}
    assert binder.validate(opt, {}).state == BIND_MISSING


# ═══════════════════════════════ cascade via route() ═══════════════════════════


def _open(monkeypatch, *, mode="off", timeout=5.0, backend="online"):
    from core.config import settings

    monkeypatch.setattr(settings, "chat_funnel_enabled", True)
    monkeypatch.setattr(settings, "chat_fast_paths_enabled", True)
    monkeypatch.setattr(settings, "chat_action_fast_path_enabled", True)
    monkeypatch.setattr(settings, "chat_matcher_mode", mode)
    monkeypatch.setattr(settings, "chat_tool_intent_backend", backend)
    monkeypatch.setattr(settings, "chat_tool_intent_timeout_seconds", timeout + 1)
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
CAP = [_entry("cap-a", corpus=(MSG,), examples=("建个目录",), parameters=_NAME_SCHEMA)]


async def test_gate_closed_cascade_is_physically_dark(monkeypatch):
    from core.config import settings

    monkeypatch.setattr(settings, "chat_matcher_mode", "off")
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


async def test_index_unavailable_is_fault_empty_set_is_business(monkeypatch, caplog):
    """Ruling 2026-09-26: a missing index is a FAULT (RECALL_UNAVAILABLE);
    an EMPTY candidate set is a normal business result — it short-circuits to
    the Agent at deepest_stage=recall and the one model hop is NOT spent.
    E2 (final semantics): only the MISS/AMBIGUOUS lane depends on the index,
    so both legs run on a non-corpus sentence."""
    _open(monkeypatch)
    req = _req()
    _, deps = _wire(monkeypatch, view=_view(CAP), index=None,
                    embedder=_Embedder([1, 0]), llm=_LLM())
    with caplog.at_level(logging.INFO, logger="core.application.chat.intent_funnel"):
        assert await funnel.route(_ctx("随便聊聊"), deps=deps, requirements=req) is req
    assert f"fallback_reason={REASON_RECALL_UNAVAILABLE}" in caplog.records[-1].getMessage()

    empty_idx = _index([])
    llm = _LLM([{"capability_id": "NONE"}])
    _, deps = _wire(monkeypatch, view=_view(CAP), index=empty_idx,
                    embedder=_Embedder([1, 0]), llm=llm)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="core.application.chat.intent_funnel"):
        assert await funnel.route(_ctx("随便聊聊"), deps=deps, requirements=req) is req
    line = caplog.records[-1].getMessage()
    assert f"fallback_reason={REASON_NO_CANDIDATE}" in line
    assert "deepest_stage=recall" in line and "tool_intent=-" in line
    assert llm.prompts == []                        # the hop was NOT spent


async def test_recall_faults_report_unavailable_never_fake_empty(monkeypatch, caplog):
    """Ruling 2026-09-26 (point 2): a system fault in recall — embedder error
    or embedder/corpus DIM mismatch (profile config error) — must report
    RECALL_UNAVAILABLE, never the business-result NO_CANDIDATE; no hop spent."""
    _open(monkeypatch)
    req = _req()
    idx = _index([("cap-a", [MSG], [[1.0, 0.0]])])

    boom_emb = _Embedder([1.0, 0.0], fail=True)
    _, deps = _wire(monkeypatch, view=_view(CAP), index=idx,
                    embedder=boom_emb, llm=_LLM())
    with caplog.at_level(logging.INFO, logger="core.application.chat.intent_funnel"):
        assert await funnel.route(_ctx("随便聊聊"), deps=deps, requirements=req) is req
    line = next(r.getMessage() for r in caplog.records if "funnel_trace" in r.getMessage())
    assert f"fallback_reason={REASON_RECALL_UNAVAILABLE}" in line
    assert "NO_CANDIDATE" not in line
    assert deps.llm.prompts == []

    # dim mismatch: 2-d query vector vs 3-d corpus row — a profile/dim config
    # fault, not a silent truncation that would fake its way to an empty set.
    wide_idx = _index([("cap-a", [MSG], [[1.0, 0.0, 0.0]])])
    llm = _LLM()
    _, deps = _wire(monkeypatch, view=_view(CAP), index=wide_idx,
                    embedder=_Embedder([1.0, 0.0]), llm=llm)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="core.application.chat.intent_funnel"):
        assert await funnel.route(_ctx("随便聊聊"), deps=deps, requirements=req) is req
    line = next(r.getMessage() for r in caplog.records if "funnel_trace" in r.getMessage())
    assert f"fallback_reason={REASON_RECALL_UNAVAILABLE}" in line
    assert "dim mismatch" in "\n".join(
        r.getMessage() for r in caplog.records if "fail-open" in r.getMessage())
    assert llm.prompts == []


async def test_matcher_hit_single_hop_certifies(monkeypatch, caplog):
    """HIT enters ToolIntentModel with the same semantics as the Recall lane: ONE
    model call (select + extract), Binder validates, no second hop."""
    _open(monkeypatch)
    llm = _LLM([{"capability_id": "cap-a", "confidence": 0.95,
                 "arguments": {"name": "季度报告"}}])
    view = _view(CAP)
    idx = _index([("cap-a", ["建个目录"], [[0.1, 0.9]])])  # recall would MISS
    _, deps = _wire(monkeypatch, view=view, index=idx,
                    embedder=_Embedder([1.0, 0.0]), llm=llm)
    req = _req()
    with caplog.at_level(logging.INFO, logger="core.application.chat.intent_funnel"):
        out = await funnel.route(_ctx(MSG), deps=deps, requirements=req)
    assert out is not req
    act = out.requested_action
    assert act["tool"] == "create_folder" and act["args"] == {"name": "季度报告"}
    assert act["capability_id"] == "cap-a"
    # live-table ruling: the ONLY Registry stamp is the content fingerprint
    # (the legacy index-version action stamp is gone from the action shape).
    assert "registry_version" not in act
    assert act["funnel_registry_version"] == view.fingerprint
    assert act["funnel_stage"] == "tool_intent"
    assert out.needs_action is Signal.HIGH and out.complexity is Complexity.LOW
    assert len(llm.prompts) == 1                       # AT MOST one ToolIntentModel call
    assert "evidence: exact standard-query match (table)" in llm.prompts[0]  # HIT provenance, label not score
    assert "matched_example" in llm.prompts[0]
    line = next(r.getMessage() for r in caplog.records if "funnel_trace" in r.getMessage())
    assert "final_route=action" in line and "fallback_reason=-" in line
    assert "tool_intent=CONFIDENT:cap-a" in line


async def test_matcher_mode_on_still_spends_the_one_model_call(monkeypatch):
    """The direct-certification special path is DELETED: a HIT is not an
    execution permit — ToolIntentModel still adjudicates and extracts."""
    _open(monkeypatch, mode="on")
    llm = _LLM([{"capability_id": "cap-a", "confidence": 0.95,
                 "arguments": {"name": "季度报告"}}])
    _, deps = _wire(monkeypatch, view=_view(CAP), index=_index([]),
                    embedder=_Embedder([1.0, 0.0]), llm=llm)
    out = await funnel.route(_ctx(MSG), deps=deps, requirements=_req())
    assert out.requested_action["funnel_stage"] == "tool_intent"
    assert out.requested_action["args"] == {"name": "季度报告"}
    assert len(llm.prompts) == 1


async def test_recall_lane_single_hop_certifies(monkeypatch):
    _open(monkeypatch)
    llm = _LLM([{"capability_id": "cap-a", "confidence": 0.9,
                 "arguments": {"name": "季度报告"}}])
    idx = _index([("cap-a", [MSG], [[1.0, 0.0]])])
    # no table alias: this is the MISS -> Recall path
    view = _view([_entry("cap-a", examples=("建个目录",), parameters=_NAME_SCHEMA)])
    _, deps = _wire(monkeypatch, view=view, index=idx,
                    embedder=_Embedder([1.0, 0.0]), llm=llm)
    out = await funnel.route(_ctx(MSG), deps=deps, requirements=_req())
    assert out.requested_action["funnel_stage"] == "tool_intent"
    assert out.requested_action["args"] == {"name": "季度报告"}
    assert len(llm.prompts) == 1
    assert "origin=recall" in llm.prompts[0]


async def test_stub_confident_without_extraction_exits_bind_missing(monkeypatch, caplog):
    """Honest consequence of the stub having no extraction power: CONFIDENT
    verdict + None draft -> validate MISSING -> straight to the Agent."""
    _open(monkeypatch, backend="stub")
    idx = _index([("cap-a", [MSG], [[1.0, 0.0]])])
    _, deps = _wire(monkeypatch, view=_view(CAP), index=idx,
                    embedder=_Embedder([1.0, 0.0]), llm=_LLM())
    req = _req()
    with caplog.at_level(logging.INFO, logger="core.application.chat.intent_funnel"):
        out = await funnel.route(_ctx(MSG), deps=deps, requirements=req)
    assert out is req
    line = next(r.getMessage() for r in caplog.records if "funnel_trace" in r.getMessage())
    assert f"fallback_reason={REASON_BIND_MISSING}" in line
    assert "deepest_stage=binder" in line


async def test_below_floor_verdict_returns_original(monkeypatch, caplog):
    _open(monkeypatch)
    llm = _LLM([{"capability_id": "cap-a", "confidence": 0.3}])   # under the floor
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
    assert f"fallback_reason={REASON_TOOL_INTENT_UNCERTAIN}" in line


async def test_tool_intent_reject_exits_tool_intent_reject(monkeypatch, caplog):
    _open(monkeypatch)
    idx = _index([("cap-a", [MSG], [[1.0, 0.0]])])
    llm = _LLM([{"capability_id": "NONE"}])
    _, deps = _wire(monkeypatch, view=_view(CAP), index=idx,
                    embedder=_Embedder([1.0, 0.0]), llm=llm)
    req = _req()
    with caplog.at_level(logging.INFO, logger="core.application.chat.intent_funnel"):
        out = await funnel.route(_ctx(MSG), deps=deps, requirements=req)
    assert out is req                                   # Agent keeps the turn
    line = next(r.getMessage() for r in caplog.records if "funnel_trace" in r.getMessage())
    assert f"fallback_reason={REASON_TOOL_INTENT_REJECT}" in line


async def test_verdict_not_in_active_table_is_version_mismatch(monkeypatch, caplog):
    _open(monkeypatch)
    idx = _index([("cap-ghost", ["x"], [[1.0, 0.0]])])   # index ahead of table
    llm = _LLM([{"capability_id": "cap-ghost", "confidence": 0.99, "arguments": {}}])
    _, deps = _wire(monkeypatch, view=_view(CAP), index=idx,
                    embedder=_Embedder([1.0, 0.0]), llm=llm)
    req = _req()
    with caplog.at_level(logging.INFO, logger="core.application.chat.intent_funnel"):
        out = await funnel.route(_ctx("x"), deps=deps, requirements=req)
    assert out is req
    line = next(r.getMessage() for r in caplog.records if "funnel_trace" in r.getMessage())
    assert f"fallback_reason={REASON_VERSION_MISMATCH}" in line


async def test_confident_but_unextractable_exits_bind_missing_no_second_hop(
        monkeypatch, caplog):
    """The old BIND_MISSING -> recheck -> Decision chain is GONE: one CONFIDENT
    verdict, one missing draft, straight to the Agent."""
    _open(monkeypatch)
    view = _view([_entry("cap-a", corpus=("新建文件夹",), parameters=_NAME_SCHEMA)])
    idx = _index([("cap-a", ["建个目录"], [[0.2, 0.8]])])
    llm = _LLM([{"capability_id": "cap-a", "confidence": 0.95}])   # no arguments
    _, deps = _wire(monkeypatch, view=view, index=idx,
                    embedder=_Embedder([1.0, 0.0]), llm=llm)
    req = _req()
    with caplog.at_level(logging.INFO, logger="core.application.chat.intent_funnel"):
        out = await funnel.route(_ctx("新建文件夹"), deps=deps, requirements=req)
    assert out is req                       # Agent owns the clarification (8.7)
    line = next(r.getMessage() for r in caplog.records if "funnel_trace" in r.getMessage())
    assert f"fallback_reason={REASON_BIND_MISSING}" in line
    assert "tool_intent=CONFIDENT:cap-a" in line and "recheck" not in line
    assert len(llm.prompts) == 1            # no second model hop was spent


async def test_invalid_draft_exits_bind_invalid(monkeypatch, caplog):
    _open(monkeypatch)
    idx = _index([("cap-a", [MSG], [[1.0, 0.0]])])
    llm = _LLM([{"capability_id": "cap-a", "confidence": 0.95,
                 "arguments": {"name": "名" * 121}}])               # over max_len
    _, deps = _wire(monkeypatch, view=_view(CAP), index=idx,
                    embedder=_Embedder([1.0, 0.0]), llm=llm)
    req = _req()
    with caplog.at_level(logging.INFO, logger="core.application.chat.intent_funnel"):
        out = await funnel.route(_ctx(MSG), deps=deps, requirements=req)
    assert out is req
    line = next(r.getMessage() for r in caplog.records if "funnel_trace" in r.getMessage())
    assert "fallback_reason=BIND_INVALID" in line


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
    # E2: the HIT lane never loads the index, so the recall-stage timeout can
    # only be reached by a MISS — a HIT here would skip slow_load entirely.
    with caplog.at_level(logging.INFO, logger="core.application.chat.intent_funnel"):
        out = await funnel.route(_ctx("随便聊聊"), deps=deps, requirements=req)
    assert out is req
    line = next(r.getMessage() for r in caplog.records if "funnel_trace" in r.getMessage())
    assert f"fallback_reason={REASON_RECALL_TIMEOUT}" in line
    assert "deepest_stage=recall" in line


async def _view_ok():
    return _view(CAP)


async def test_negated_matcher_hit_is_forced_to_miss(monkeypatch, caplog):
    _open(monkeypatch, mode="on")
    # the corpus itself contains the negated sentence: an exact HIT WOULD fire —
    # only the 8.1-a guard can veto it. With the HIT vetoed and recall empty,
    # the model-facing set is empty -> NO_CANDIDATE short-circuit (ruling
    # 2026-09-26); the hop is never spent on a turn with no card to select.
    view = _view([_entry("cap-a", corpus=("不要新建文件夹",))])
    llm = _LLM([{"capability_id": "NONE"}])
    _, deps = _wire(monkeypatch, view=view, index=_index([]),
                    embedder=_Embedder([1.0, 0.0]), llm=llm)
    req = _req()
    with caplog.at_level(logging.INFO, logger="core.application.chat.intent_funnel"):
        out = await funnel.route(_ctx("不要新建文件夹"), deps=deps, requirements=req)
    assert out is req
    line = next(r.getMessage() for r in caplog.records if "funnel_trace" in r.getMessage())
    assert "matcher=MISS" in line
    assert f"fallback_reason={REASON_NO_CANDIDATE}" in line
    assert llm.prompts == []


async def test_ambiguous_carries_all_candidates_into_the_one_call(monkeypatch):
    _open(monkeypatch)
    v = _view([_entry("cap-a", corpus=("季度汇总",), parameters=_NAME_SCHEMA),
               _entry("cap-b", tool="add_term", corpus=("季度汇总",),
                      parameters=_TERM_SCHEMA)])
    llm = _LLM([{"capability_id": "cap-b", "confidence": 0.9,
                 "arguments": {"term": "季度汇总", "domain": "财务"}}])
    _, deps = _wire(monkeypatch, view=v, index=_index([]),
                    embedder=_Embedder([1.0, 0.0]), llm=llm)
    out = await funnel.route(_ctx("季度汇总"), deps=deps, requirements=_req())
    assert len(llm.prompts) == 1                        # ONE call disambiguates
    assert "cap-a" in llm.prompts[0] and "cap-b" in llm.prompts[0]  # BOTH carried up
    assert "evidence: exact standard-query match (table; several candidates)" in llm.prompts[0]
    assert out.requested_action["capability_id"] == "cap-b"
    assert out.requested_action["args"] == {"term": "季度汇总", "domain": "财务"}


# ═══════════ E1/E2 — final semantics landing (2026-09-26 audit) ═════════════════


def test_aggregate_by_capability_keeps_the_winning_candidate():
    """E1 unit: ONE capability-level candidate per capability_id — the highest-
    scoring hit rides WITH ITS OWN provenance; ties keep the earlier arrival
    (the matcher seed is seeded first, so table evidence wins a tie); distinct
    capabilities are all preserved (no top_k, no dropping)."""
    from core.application.chat.intent_funnel.funnel import _aggregate_by_capability

    low = Candidate("cap-a", 0.89, matched_example="低", query_id="q2")
    win = Candidate("cap-a", 0.94, matched_example="高", query_id="q1")
    other = Candidate("cap-b", 0.86, matched_example="图", query_id="q3")
    amb = Candidate("cap-a", 0.0, origin="matcher_ambiguous")
    out = _aggregate_by_capability([amb, low, win, other])
    assert len(out) == 2                       # summary x3 + mindmap -> summary + mindmap
    assert out[0] is win and out[1] is other   # max rides with its provenance
    t1 = Candidate("cap-a", 0.90, matched_example="先")
    t2 = Candidate("cap-a", 0.90, matched_example="后")
    assert _aggregate_by_capability([t1, t2])[0] is t1


async def test_capability_aggregation_collapses_duplicate_hits_into_one_card(monkeypatch):
    """E1 integration: Raw Recall keeps every >= threshold hit (pinned at the
    recall node above), but the model-facing list is capability-level — cap-a
    arrives twice (cos 1.000 + 0.981), the prompt carries ONE cap-a card at the
    WINNING score/provenance, and cap-b is untouched."""
    _open(monkeypatch)
    llm = _LLM([{"capability_id": "cap-a", "confidence": 0.9,
                 "arguments": {"name": "季度报告"}}])
    idx = _index([("cap-a", ["近a", "远b"], [[1.0, 0.0], [1.0, 0.2]]),
                  ("cap-b", ["加个词"], [[1.0, 0.4]])])
    view = _view([_entry("cap-a", examples=("做个事",), parameters=_NAME_SCHEMA),
                  _entry("cap-b", tool="add_term", examples=("写个词",),
                         parameters=_TERM_SCHEMA)])
    _, deps = _wire(monkeypatch, view=view, index=idx,
                    embedder=_Embedder([1.0, 0.0]), llm=llm)
    out = await funnel.route(_ctx("随便聊聊"), deps=deps, requirements=_req())
    prompt = llm.prompts[0]
    assert len(llm.prompts) == 1
    assert prompt.count("### cap-a") == 1        # 1.000 + 0.981 collapsed to one
    assert prompt.count("### cap-b") == 1        # the different capability survives
    assert "score=1.000" in prompt and "score=0.981" not in prompt
    assert "近a" in prompt and "远b" not in prompt  # winning provenance rides alone
    assert out.requested_action["capability_id"] == "cap-a"


async def test_matcher_hit_never_touches_the_recall_index(monkeypatch, caplog):
    """E2 (final semantics): an exact HIT certifies INDEPENDENTLY of Recall
    availability — the HIT lane never calls load_index, so an unembedded or
    faulting corpus can never veto a table-proven turn. The MISS lane keeps
    the RECALL_UNAVAILABLE semantics (pinned above)."""
    _open(monkeypatch)
    llm = _LLM([{"capability_id": "cap-a", "confidence": 0.95,
                 "arguments": {"name": "季度报告"}}])
    _, deps = _wire(monkeypatch, view=_view(CAP), index=None,
                    embedder=_Embedder([1.0, 0.0]), llm=llm)

    async def boom_load(sf):
        raise AssertionError("E2: the HIT lane must never load the Recall index")

    monkeypatch.setattr(
        "core.application.chat.intent_funnel.recall.load_index", boom_load)
    with caplog.at_level(logging.INFO, logger="core.application.chat.intent_funnel"):
        out = await funnel.route(_ctx(MSG), deps=deps, requirements=_req())
    assert out.requested_action["capability_id"] == "cap-a"
    assert len(llm.prompts) == 1
    line = next(r.getMessage() for r in caplog.records if "funnel_trace" in r.getMessage())
    assert "index_version=-" in line and "final_route=action" in line
