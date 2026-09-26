"""§8.16 Golden Set runner — executes the validation-gate regression matrix in
``tests/golden/intent_funnel_golden.yaml`` against the REAL single-hop cascade
(Matcher HIT / Recall -> ONE ToolIntentModel call -> Binder validate -> certified).

Design of the fake world (kept deterministic on purpose — a golden that can
flap is worse than no golden):

* the LIVE table carries exactly the four capabilities the matrix names:
  ``cap-folder`` (Standard query row + curated Similar rows; a legacy ``re:``
  pattern stays in storage as INERT proof — the exact-only Matcher (ruling
  2026-09-25) never reads it; corpus vector [1,0]),
  ``cap-vocab`` (own Standard + the shared multi-intent Similar — two caps
  curating one sentence is what produces MATCH_AMBIGUOUS now),
  ``cap-private`` / ``cap-web`` (exact Standards behind widened kind gates);
* the embedder is a vector map: the two sanctioned paraphrases score 1.0
  against cap-folder, EVERYTHING else falls to [0.7,0.7] — cosine 0.707
  against either axis, under the 0.82 quality gate. Recall therefore never
  "resupplies" a turn the Matcher was told to miss (the negation case leans
  on exactly this);
* the ToolIntentModel hop is a SCRIPTED deterministic stand-in (backend pinned
  "online", deps.llm = _ScriptedToolIntent): it parses its own card prompt, keeps
  the single-candidate rule, and extracts by quote-stripping — the same
  contract a deployed small model serves, made flap-free. Its scripted NONE on
  a split card set is the only negative the matrix exercises;
* every run is pinned ``execution_mode="test"`` (8.14: a batch of goldens must
  never land cost on a user) and the embedder PROVES the pin rode every call.

Expectations speak the YAML's 8.10 vocabulary; ``FUNNEL_*`` tokens resolve to
the contract constants here so the YAML stays readable while the assertion
compares against the value actually written to the funnel_trace line.
"""
from __future__ import annotations

import logging
import re
import types
from pathlib import Path

import pytest
import yaml
from core.application.chat.intent_funnel import funnel
from core.application.chat.intent_funnel.contract import (
    REASON_BIND_MISSING,
    REASON_KIND_DISABLED,
    REASON_TOOL_INTENT_REJECT,
)
from core.application.chat.intent_funnel.registry import content_fingerprint
from core.application.chat.intent_funnel.registry.entry import (
    KIND_ACTION,
    KIND_PRIVATE,
    KIND_WEB,
    CapabilityEntry,
    QueryRecord,
    RegistryLiveView,
    derive_language,
)
from core.infrastructure.request_context import (
    get_request_execution_mode,
    reset_request_execution_mode,
    set_request_execution_mode,
)

FUNNEL_LOGGER = "core.application.chat.intent_funnel.funnel"
GOLDEN_PATH = Path(__file__).parent / "golden" / "intent_funnel_golden.yaml"

# YAML token -> the contract constant actually logged as fallback_reason.
# FUNNEL_NO_CANDIDATE is retired (ruling 2026-09-25): an empty candidate set
# now reaches ToolIntentModel, whose NONE lands on FUNNEL_TOOL_INTENT_REJECT.
FALLBACK_CODES = {
    "FUNNEL_TOOL_INTENT_REJECT": REASON_TOOL_INTENT_REJECT,
    "FUNNEL_BIND_MISSING": REASON_BIND_MISSING,
    "FUNNEL_KIND_DISABLED": REASON_KIND_DISABLED,
}

MSG_FOLDER = '新建文件夹"季度报告"'
MSG_BARE_FOLDER = "新建文件夹"
MSG_PRIVATE = '创建文件夹"私密日记"'
MSG_WEB = "查一下这个词的词源"
MSG_VOCAB_EXAMPLE = '把"keystone"加入我的工程词汇库'
# ONE sentence curated under BOTH action caps — the only way exact-only
# matching can ever produce MATCH_AMBIGUOUS
MSG_MULTI_INTENT = '新建文件夹"季度报告"并把"keystone"加入我的工程词汇库'

# the ONLY paraphrases the fake corpus embeds near cap-folder (cos 1.0);
# everything else falls to [0.7,0.7] -> 0.707 < min_score 0.82 -> no candidate
PARAPHRASE_VECTORS = {
    '创建文件夹"资料归档"': [1.0, 0.0],
    'create a folder named "Read Later"': [1.0, 0.0],
}


def _load_cases() -> list[dict]:
    doc = yaml.safe_load(GOLDEN_PATH.read_text(encoding="utf-8"))
    return doc["cases"]


CASES = _load_cases()


def _q(qid, text, *, parent=None, position=0):
    return QueryRecord(id=qid, query=text, language=derive_language(text),
                       position=position, standard_query_id=parent)


def _table() -> tuple[CapabilityEntry, ...]:
    return (
        CapabilityEntry(
            capability_id="cap-folder", tool_binding="create_folder",
            description="新建一个带引号名称的文件夹。",
            standard_queries=(_q("s1", MSG_FOLDER),),
            similar_queries=(_q("m1", MSG_BARE_FOLDER, parent="s1", position=1),
                             _q("m2", MSG_MULTI_INTENT, parent="s1", position=2)),
            # INERT legacy storage: the exact-only Matcher never reads these
            # (ruling 2026-09-25) — kept here so the goldens prove it
            patterns=("re:新建文件夹",), aliases=(MSG_FOLDER,),
            request_query_examples=(MSG_FOLDER,),
            parameters={"name": {"type": "string", "required": True,
                                 "max_len": 120, "description": "folder name"}},
            arg_slots={"name": {"source": "user_input"}},
            intent_kind=KIND_ACTION,
        ),
        CapabilityEntry(
            capability_id="cap-vocab", tool_binding="add_term",
            description="把一个词加入指定领域的词汇库。",
            standard_queries=(_q("s2", MSG_VOCAB_EXAMPLE),),
            similar_queries=(_q("m3", MSG_MULTI_INTENT, parent="s2"),),
            patterns=("re:加入我的.*词汇库",), aliases=(),
            request_query_examples=(MSG_VOCAB_EXAMPLE,),
            parameters={"term": {"type": "string", "required": True,
                                 "max_len": 120, "description": "the term"},
                        "domain": {"type": "string", "required": True,
                                   "max_len": 60, "description": "vocabulary domain"}},
            arg_slots={"term": {"source": "user_input"},
                       "domain": {"source": "user_input"}},
            intent_kind=KIND_ACTION,
        ),
        CapabilityEntry(
            capability_id="cap-private", tool_binding="create_folder",
            description="在私有空间创建文件夹(演示 private kind 开关)。",
            standard_queries=(_q("s3", MSG_PRIVATE),),
            patterns=(), aliases=(),
            parameters={"name": {"type": "string", "required": True,
                                 "max_len": 120, "description": "folder name"}},
            arg_slots={"name": {"source": "user_input"}},
            intent_kind=KIND_PRIVATE,
        ),
        CapabilityEntry(
            capability_id="cap-web", tool_binding="web_search",
            description="查询词源等外部知识(演示 web kind 开关)。",
            standard_queries=(_q("s4", MSG_WEB),),
            patterns=(), aliases=(),
            parameters={"query": {"type": "string", "required": True,
                                  "max_len": 200, "description": "search request"}},
            intent_kind=KIND_WEB,
        ),
    )


def _view():
    entries = _table()
    return RegistryLiveView(
        fingerprint=content_fingerprint(list(entries)), entries=entries,
    )


def _index():
    # the LIVE Recall corpus: only the two ACTION capabilities carry embedded
    # rows; private/web are exact-Standard rows by design (backfill aside).
    return types.SimpleNamespace(
        version="corpus1-golden",
        corpus=(
            types.SimpleNamespace(
                kind="standard", query_id="s1", capability_id="cap-folder",
                query=MSG_FOLDER, language="zh", standard_query_id=None,
                vector=[1.0, 0.0]),
            types.SimpleNamespace(
                kind="standard", query_id="s2", capability_id="cap-vocab",
                query=MSG_VOCAB_EXAMPLE, language="zh", standard_query_id=None,
                vector=[0.0, 1.0]),
        ),
    )


class _Embedder:
    """Vector map + 8.14 witness: records the execution mode live at each call."""

    def __init__(self):
        self.seen: list[str] = []

    async def embed(self, texts):
        self.seen.append(get_request_execution_mode())
        return [PARAPHRASE_VECTORS.get(t, [0.7, 0.7]) for t in texts]


def _ctx(query: str, ctx_body: dict):
    viewer = None
    if "viewer_page" in ctx_body:
        viewer = types.SimpleNamespace(
            page=ctx_body["viewer_page"], asset_id="a-golden", selections=[])
    attach = {"asset_id": "att-1"} if ctx_body.get("attach") else None
    return types.SimpleNamespace(
        body=types.SimpleNamespace(message=query, attach=attach, viewer=viewer),
        owned_asset_id=None, research_turn=False, effective_handoff=None,
        session_id=ctx_body.get("session", ""),
    )


class _ScriptedToolIntent:
    """Deterministic stand-in for ToolIntentModel (the online seam's fake llm): parses
    its own card prompt, applies the single-card rule, and extracts arguments
    by quote-stripping — the same contract a deployed small model serves, with
    zero flapping. A split card set gets the honest NONE (-> TOOL_INTENT_REJECT)."""

    def __init__(self):
        self.calls = 0

    async def complete_json(self, prompt, **kw):
        self.calls += 1
        caps = re.findall(r"(?m)^### (\S+)$", prompt)
        m = re.search(r"<user_sentence>(.*?)</user_sentence>", prompt, re.DOTALL)
        sentence = m.group(1) if m else ""
        if len(caps) != 1:
            return {"capability_id": "NONE", "confidence": 1.0, "arguments": {}}
        cap = caps[0]
        quoted = re.search(r'"([^"]+)"', sentence)
        if cap in ("cap-folder", "cap-private"):
            args = {"name": quoted.group(1)} if quoted else {}
        elif cap == "cap-vocab":
            args = {"term": quoted.group(1)} if quoted else {}
            dom = re.search(r"我的(.+?)词汇库", sentence)
            if dom:
                args["domain"] = dom.group(1)
        else:  # cap-web
            args = {"query": sentence}
        return {"capability_id": cap, "confidence": 0.95, "arguments": args}


def _wire(monkeypatch, embedder: _Embedder):
    from core.config import settings

    view, index = _view(), _index()

    async def fake_active(**kw):
        return view

    async def fake_load(sf):
        return index

    monkeypatch.setattr(
        "core.application.chat.intent_funnel.registry.active_view", fake_active)
    monkeypatch.setattr(
        "core.application.chat.intent_funnel.recall.load_index", fake_load)
    monkeypatch.setattr(settings, "chat_funnel_enabled", True)
    monkeypatch.setattr(settings, "chat_fast_paths_enabled", True)
    monkeypatch.setattr(settings, "chat_action_fast_path_enabled", True)
    # ToolIntentModel rides the online seam with the scripted double; every channel/
    # floor config is pinned so a dev .env can never flap a golden.
    monkeypatch.setattr(settings, "chat_tool_intent_backend", "online")
    monkeypatch.setattr(settings, "chat_tool_intent_min_confidence", 0.75)
    monkeypatch.setattr(settings, "chat_tool_intent_online_model", "")
    monkeypatch.setattr(settings, "chat_tool_intent_online_base_url", "")
    monkeypatch.setattr(settings, "chat_tool_intent_online_api_key", "")
    monkeypatch.setattr(settings, "chat_funnel_timeout_seconds", 5.0)
    # pin the ladder geometry so an env-tweaked default can never flap a golden
    monkeypatch.setattr(settings, "chat_funnel_top_k", 3)
    monkeypatch.setattr(settings, "chat_funnel_min_score", 0.82)
    return view, types.SimpleNamespace(
        session_factory=None, embedder=lambda: embedder, llm=_ScriptedToolIntent(),
    )


def _trace_line(caplog) -> str:
    lines = [r.getMessage() for r in caplog.records
             if r.name == FUNNEL_LOGGER and "funnel_trace" in r.getMessage()]
    assert len(lines) == 1, f"expected exactly one funnel_trace line, got {lines}"
    return lines[0]


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
async def test_golden_case(monkeypatch, caplog, case):
    from core.config import settings

    settings_kw = case.get("settings", {})
    monkeypatch.setattr(settings, "chat_matcher_mode", settings_kw.get("mode", "off"))
    monkeypatch.setattr(settings, "chat_funnel_private_enabled",
                        bool(settings_kw.get("private", False)))
    monkeypatch.setattr(settings, "chat_funnel_web_enabled",
                        bool(settings_kw.get("web", False)))

    embedder = _Embedder()
    _view_obj, deps = _wire(monkeypatch, embedder)
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
    ctx = _ctx(case["query"], case.get("ctx", {}))

    caplog.set_level(logging.INFO, logger=FUNNEL_LOGGER)
    token = set_request_execution_mode("test")
    try:
        out = await funnel.route(ctx, deps=deps, requirements=requirements)
    finally:
        reset_request_execution_mode(token)

    # 8.14: every embedding call the matrix made was TEST usage, and the pin
    # died with the case (no leakage into the next parametrize row).
    assert all(m == "test" for m in embedder.seen)
    assert get_request_execution_mode() == "production"

    trace = _trace_line(caplog)
    m_field = re.search(r"matcher=(\S+)", trace).group(1)
    fb_field = re.search(r"fallback_reason=(\S+)", trace).group(1)

    expect = case["expect"]
    if "matcher_contains" in expect:
        assert expect["matcher_contains"] in m_field, trace

    if expect["route"] == "action":
        assert deps.llm.calls == 1                  # the ONE ToolIntentModel hop per certified turn
        assert out is not requirements              # certified: a NEW object
        act = out.requested_action
        assert act["capability_id"] == expect["capability"], trace
        if "tool" in expect:
            assert act["tool"] == expect["tool"]
        if "stage" in expect:
            assert act["funnel_stage"] == expect["stage"], trace
        if "kind" in expect:
            assert act["funnel_kind"] == expect["kind"]
        assert "fallback" not in expect or fb_field == "-"
    else:
        assert out is requirements                      # 8.10: byte-identical hand-off
        assert out.requested_action is None
        assert fb_field == FALLBACK_CODES[expect["fallback"]], trace

    # Whenever the Matcher did NOT certify directly, the cascade went through
    # Recall — which embeds. A golden that silently skipped Recall has changed
    # shape and must fail loudly here, not quietly stop covering the node.
    if not m_field.startswith("HIT"):
        assert embedder.seen, f"expected recall to embed for case {case['id']}"


# ── rollback leg of 8.16: a rollback must not launder the kind gate ───────────────


def test_rollback_payload_round_trip_preserves_the_intent_kind():
    """Rollback stages the OLD payload byte-identically (pinned in
    test_intent_registry). This closes the loop on the READ side: a private
    row re-materialized from that payload is still private — the kind gate
    re-applies after a rollback exactly as it did before it."""
    for entry in _table():
        back = CapabilityEntry.from_payload(entry.to_payload())
        assert back.intent_kind == entry.intent_kind
        assert back.capability_id == entry.capability_id
        assert back.patterns == entry.patterns and back.aliases == entry.aliases

    # an old (P2-era) payload without the field defaults to ACTION — the gate
    # can never be opened by omitting a key.
    legacy = dict(_table()[0].to_payload())
    legacy.pop("intent_kind", None)
    assert CapabilityEntry.from_payload(legacy).intent_kind == KIND_ACTION
