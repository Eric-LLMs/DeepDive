"""Layer A — pure control-plane routing coverage (no I/O, no LLM, no HTTP).

Every case drives the REAL :func:`resolve_requirements` → :func:`build_execution_plan`
pair (the exact functions the live ``TurnOrchestrator`` calls) and asserts the resolved
``PlanKind`` + ``source_policy`` + (for ACTION) the certified ``{tool,args}``. This is
where the bulk of the matrix lives: it is deterministic, sub-millisecond, and pins the
recognition policy for all ten charter categories without touching the kernel.

Layer-B (:mod:`test_p5_smoke` and the ``test_p5b_*`` files) then re-runs a
representative slice through the real ``/chat/stream`` + funnel to prove the plan is
EXECUTED as planned.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from core.config import settings

from tests.p5_validation._p5_harness import FakeBody, RoutingCtx, plan_for

# ── gate bundles ────────────────────────────────────────────────────────────────
FULL = {"fast": True, "direct": True, "viewer": True, "retrieval": True,
        "action": True, "composite": True}
P5A = {**FULL, "composite": False}
DARK = {**FULL, "fast": False}
ONLY_ACTION = {"fast": True, "direct": False, "viewer": False, "retrieval": False,
               "action": True, "composite": False}


def _viewer(*, status="injected", kinds=("selection",), image=False):
    blocks = [
        SimpleNamespace(kind=k, image_asset_id=("img1" if image and i == 0 else None))
        for i, k in enumerate(kinds)
    ]
    return {"status": status, "blocks": blocks}


def _ctx(message, *, viewer=None, attach=None, owned=None, research=False, handoff=None):
    body = FakeBody(message=message, attach=attach, handoff=handoff)
    return RoutingCtx(
        body=body, viewer_assembly=viewer, research_turn=research,
        effective_handoff=handoff, owned_asset_id=owned,
    )


def route(message, *, gates=FULL, **ctxf):
    reqs, plan = plan_for(_ctx(message, **ctxf), message, **gates)
    return plan.kind.value, plan, reqs


# ════════════════════════════════════════════════════════════════════════════════
# 1. DIRECT — short, pure, zero capability demand
# ════════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("msg", [
    "hello there",
    "what is 2 + 2?",
    "thanks!",
    "tell me a joke",
    "你好呀",
    "你是谁",
    "帮我润色这句话：今天天气不错",  # no reserved action/web verb, pure, short
    "rephrase: the cat sat",
    "is the earth flat",
    "give me a haiku about rain",
    "what does 'serendipity' mean",
    "总结一下这句话的核心观点",  # '总结' is not an _ACTION_PAT verb
])
def test_direct_pure_turns(msg):
    assert route(msg)[0] == "direct"


@pytest.mark.parametrize("msg", [
    "hello", "hi", "你好", "good morning", "what's the capital of France",
])
def test_direct_off_routes_agent(msg):
    # A pure short turn is only direct when the gate is on; OFF ⇒ Agent (dark launch).
    assert route(msg, gates=dict(FULL, direct=False))[0] == "agent"


# ════════════════════════════════════════════════════════════════════════════════
# 2. WEB / time-sensitive → never direct, never a fast path (Agent owns web_search)
# ════════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("msg", [
    "what is the weather today",
    "latest news about AI",
    "current price of bitcoin",
    "what's the score right now",
])
def test_web_demand_stays_on_agent(msg):
    kind, _, reqs = route(msg)
    assert kind == "agent"
    assert reqs.needs_web.value == "high"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "VALIDATED FINDING (Phase-2 _WEB_PAT, not 5A): the CJK time words (今天/最新/版本/"
        "股价…) are wrapped in a single \\b(...|\\u4eca\\u5929|...)\\b group, but CJK chars are "
        "\\w so \\b never fires mid-phrase — Chinese temporal queries silently miss "
        "needs_web and can be answered tool-less/stale via DIRECT. EN words work; \"今天 天气\" "
        "(space-separated) works. Fix = split EN \\b group from the un-\\b'd CJK alternation; "
        "left for report/user sign-off due to Phase-2 routing-shift risk."
    ),
)
@pytest.mark.parametrize("msg", ["今天天气怎么样", "python 最新版本是什么", "现在几点了"])
def test_cjk_web_demand_should_stay_on_agent(msg):
    _, _, reqs = route(msg)
    assert reqs.needs_web.value == "high"


# ════════════════════════════════════════════════════════════════════════════════
# 3. VIEWER — grounded over ALREADY-INJECTED text
# ════════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("msg", [
    "summarize this passage",
    "what is the main argument here",
    "explain the selected text",
    "这段内容讲了什么",
    "translate the selection to French",  # 'translate' is _OTHER_DEMAND but not an L0 web/action signal
])
def test_viewer_injected_text(msg):
    assert route(msg, viewer=_viewer(kinds=("full_text",)))[0] == "viewer"


def test_viewer_stub_is_agent():
    # Open (status "stub") ≠ Injected: read_document authority stays with the Agent.
    assert route("what does this document say", viewer=_viewer(status="stub"))[0] == "agent"


def test_viewer_image_block_is_agent():
    # roi/frame/image blocks need the vision tool — never a tool-less viewer answer.
    assert route("describe this region", viewer=_viewer(kinds=("roi",), image=True))[0] == "agent"


def test_viewer_plus_private_is_composite_only_when_gate_on():
    msg = "compare this passage with my notes on the topic"
    assert route(msg, viewer=_viewer(), gates=FULL)[0] == "composite"
    assert route(msg, viewer=_viewer(), gates=P5A)[0] == "agent"  # 5B OFF


def test_viewer_plus_action_demand_is_agent():
    # A co-occurring tool action disqualifies the pure viewer answer.
    assert route(
        "save this selection to a folder", viewer=_viewer()
    )[0] == "agent"


# ════════════════════════════════════════════════════════════════════════════════
# 4. LOCAL_RAG — private corpus as the SOLE demand
# ════════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("msg", [
    "what does my knowledge base say about gradient descent",
    "search my documents for backprop",  # 'search' here is not the _WEB word; my documents → private
    "根据我的笔记，解释一下注意力机制",
    "do my notes mention dropout regularization",
    "summarize the key points in my library about transformers",
])
def test_private_sole_demand_is_rag(msg):
    kind, plan, _ = route(msg, gates=P5A)
    assert kind == "local_rag"
    # A private-first turn fences escalated sources to the corpus.
    assert plan.source_policy in ("private_first", "private_only")


def test_private_rag_off_is_agent():
    assert route("what do my documents say about x", gates=dict(P5A, retrieval=False))[0] == "agent"


def test_private_plus_web_is_agent():
    # A mixed {private + web} demand is the Agent's to arbitrate, never a RAG path.
    assert route("compare my notes with the latest news on this")[0] == "agent"


# ════════════════════════════════════════════════════════════════════════════════
# 5. ACTION — Phase 5A typed dispatch (certified single registered tool)
# ════════════════════════════════════════════════════════════════════════════════
CREATE_OK = [
    ('create a folder named "archive"', "archive"),
    ('make a folder "projects"', "projects"),
    ('create the folder called "notes"', "notes"),
    ('make me a new directory titled "tmp"', "tmp"),
    ("创建文件夹“资料”", "资料"),
    ("新建一个叫“临时”的文件夹", "临时"),
    ("建立一个名为“归档”的目录", "归档"),
    ('create a folder named 「work」', "work"),  # CJK corner quotes
    ('make a folder ‘draft’', "draft"),  # curly single quotes
]


@pytest.mark.parametrize("msg,expected", CREATE_OK)
def test_action_create_folder_certified(msg, expected):
    kind, plan, _ = route(msg, gates=ONLY_ACTION)
    assert kind == "action"
    assert plan.action["tool"] == "create_folder"
    assert plan.action["args"] == {"name": expected}


ADD_OK = [
    ('add "quantum" to my science vocab', "quantum", "science"),
    ('add "entropy" to the physics vocabulary', "entropy", "physics"),
    ("把“熵”加入我的物理词库", "熵", "物理"),
    ("将“量子”添加到化学单词库", "量子", "化学"),
    ('add "lambda" to my programming word list', "lambda", "programming"),
]


@pytest.mark.parametrize("msg,term,domain", ADD_OK)
def test_action_add_term_certified(msg, term, domain):
    kind, plan, _ = route(msg, gates=ONLY_ACTION)
    assert kind == "action"
    assert plan.action["tool"] == "add_term"
    assert plan.action["args"] == {"term": term, "domain": domain}


def test_action_pdf_extract_needs_asset_context():
    msg = "extract the text from this document"
    # No attach / owned asset ⇒ asset_id undetermined ⇒ abstain (Agent).
    assert route(msg, gates=ONLY_ACTION)[0] == "agent"
    # With a context asset the single-shot atomic tool certifies.
    kind, plan, _ = route(msg, gates=ONLY_ACTION, owned="asset-9")
    assert kind == "action"
    assert plan.action == {"tool": "pdf_extract_text", "args": {"asset_id": "asset-9"}}


def test_action_pdf_extract_zh_with_attach():
    kind, plan, _ = route(
        "帮我把这篇文档提取全文", gates=ONLY_ACTION,
        attach={"asset_id": "att-1"},
    )
    assert kind == "action"
    assert plan.action["tool"] == "pdf_extract_text"


# Recognition-failure ⇒ LOSSLESS fallback to the Agent (never a guess).
@pytest.mark.parametrize("msg", [
    'create a folder named archive',          # unquoted slot
    'create a folder',                        # no name at all
    'make a folder named "a" and delete "b"',  # compound second demand
    'create a folder named "x" then search the web',  # sequencing + web
    "add to my vocab",                        # missing term slot
    'add "x" to my vocab',                    # empty domain
])
def test_action_recognition_failures_fall_back(msg):
    assert route(msg, gates=ONLY_ACTION)[0] == "agent"


def test_action_gate_off_is_agent():
    assert route('create a folder named "z"', gates=dict(ONLY_ACTION, action=False))[0] == "agent"


def test_action_with_web_demand_falls_back():
    # needs_web HIGH blocks the ACTION certification (single-capability contract).
    kind, _, _ = route('create a folder named "x" and check the latest news', gates=ONLY_ACTION)
    assert kind == "agent"


def test_action_fences_nothing_even_if_private_phrase_present():
    # ACTION is not a retrieval path: an unrelated private word must NOT sink a fence.
    _, plan, _ = route('create a folder named "kb-notes"', gates=ONLY_ACTION)
    assert plan.kind.value == "action"
    assert plan.source_policy is None


# ════════════════════════════════════════════════════════════════════════════════
# 6. COMPOSITE (5B) — static independent {viewer text + private recall}, no sequencing
# ════════════════════════════════════════════════════════════════════════════════
def test_composite_independent_pair():
    msg = "connect the selection above with what my notes say on it"
    kind, plan, _ = route(msg, viewer=_viewer(), gates=FULL)
    assert kind == "composite"
    assert plan.requires_viewer and plan.requires_retrieval


@pytest.mark.parametrize("seq", [
    "结合这段内容，然后查我的笔记里相关的部分",
    "summarize the selection and then look it up in my knowledge base",
])
def test_composite_never_on_sequences(seq):
    # A sequencing word means the 2nd step consumes the 1st's output — that is the Agent's.
    assert route(seq, viewer=_viewer(), gates=FULL)[0] == "agent"


def test_composite_off_is_agent():
    msg = "connect this selection with my notes"
    assert route(msg, viewer=_viewer(), gates=P5A)[0] == "agent"


def test_composite_source_policy_is_private_first():
    msg = "combine the passage with my library notes"
    _, plan, _ = route(msg, viewer=_viewer(), gates=FULL)
    assert plan.source_policy == "private_first"


# ════════════════════════════════════════════════════════════════════════════════
# 7. AGENT fallback — dynamic dependencies / multi-hop / research / handoff
# ════════════════════════════════════════════════════════════════════════════════
def test_research_turn_is_forced_complex_agent():
    kind, _, reqs = route("outline a plan", research=True)
    assert kind == "agent"
    assert reqs.complexity.value == "complex"


def test_handoff_turn_is_forced_complex_agent():
    kind, _, reqs = route("continue where you left off", handoff={"task": "t"})
    assert kind == "agent"
    assert reqs.needs_action.value == "high"


def test_multi_tool_compound_is_agent():
    assert route('create a folder named "x" and add "y" to my science vocab')[0] == "agent"


def test_overlong_message_not_direct():
    long_msg = "summarize the philosophy of " * 40  # > 400 chars
    assert route(long_msg)[0] == "agent"


def test_jsonish_turn_not_pure_direct():
    assert route('{"cmd": "hello"}')[0] == "agent"


# ════════════════════════════════════════════════════════════════════════════════
# 8. SOURCE POLICY — derived from the ORIGINAL request, never from a fast-path failure
# ════════════════════════════════════════════════════════════════════════════════
def test_private_only_hard_fence():
    _, plan, reqs = route("answer only from my knowledge base what backprop is")
    assert reqs.private_only is True
    assert plan.source_policy == "private_only"


def test_explicit_external_ok_clears_fence():
    # "you may search the web if you can't find it" re-opens external sources.
    _, plan, _ = route(
        "what do my notes say about x — if not found, you may search the web",
    )
    # A live web demand disqualifies the pure-RAG path → Agent, with no private fence.
    assert plan.source_policy is None


def test_private_first_when_no_external_permission():
    _, plan, _ = route("what does my corpus say about attention")
    assert plan.source_policy == "private_first"


def test_private_only_co_occurring_web_is_not_a_fence():
    # Restriction + web demand is a contradiction → abstain, keep the normal funnel.
    _, plan, reqs = route("only from my knowledge base, but also the latest news please")
    assert reqs.private_only is False
    assert plan.source_policy is None


# ════════════════════════════════════════════════════════════════════════════════
# 9. Regression / dark launch — master gate OFF is indistinguishable from legacy
# ════════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("msg,ctxf", [
    ('create a folder named "a"', {}),
    ("what does my knowledge base say about x", {}),
    ("hello", {}),
    ("summarize the selection", {"viewer": _viewer()}),
    ("weather today", {}),
])
def test_master_gate_off_everything_is_agent(msg, ctxf):
    assert route(msg, gates=DARK, **ctxf)[0] == "agent"


@pytest.mark.parametrize("gate", ["direct", "viewer", "retrieval", "action", "composite"])
def test_each_gate_is_independently_sufficient_to_disable(gate):
    # Turning one path off never misroutes the others' certifying turns to IT;
    # a create-folder still reaches ACTION with all-but-its-own gate on.
    if gate == "action":
        assert route('create a folder named "a"', gates=dict(P5A, action=False))[0] == "agent"
    else:
        # action turn is unaffected by the other four gates
        assert route('create a folder named "a"', gates=dict(P5A, **{gate: False}))[0] == "action"


def test_full_vs_p5a_action_parity():
    msg = 'create a folder named "shared"'
    assert route(msg, gates=FULL)[0] == route(msg, gates=ONLY_ACTION)[0] == "action"


# ════════════════════════════════════════════════════════════════════════════════
# 10. Boundary matrix — tool-phrasing breadth, viewer content kinds, source-policy
#     combinations, cross-tool ambiguity. Data-driven so each row is an independent
#     case; every expectation is derived from the verified L0/regex contract.
# ════════════════════════════════════════════════════════════════════════════════
ANY = object()


def _v(kind):  # one injected text block of the given kind
    return _viewer(kinds=(kind,))


# (case-id, message, ctx-kwargs, gates, expected_kind, expected_action_tool, expected_source_policy)
BOUNDARY = [
    # ── create_folder phrasing breadth → ACTION(name) ─────────────────────────────
    ("cf01", 'create folder "alpha"', {}, ONLY_ACTION, "action", "create_folder", None),
    ("cf02", 'make me a folder titled "beta"', {}, ONLY_ACTION, "action", "create_folder", None),
    ("cf03", 'create a new directory named "gamma"', {}, ONLY_ACTION, "action", "create_folder", None),
    ("cf04", "建立文件夹「delta」", {}, ONLY_ACTION, "action", "create_folder", None),
    ("cf05", '创建一个叫"epsilon"的目录', {}, ONLY_ACTION, "action", "create_folder", None),
    ("cf06", 'can you create a folder named "zeta" please', {}, ONLY_ACTION, "action", "create_folder", None),
    ("cf07", 'make a folder named "has space"', {}, ONLY_ACTION, "action", "create_folder", None),
    # ── add_term phrasing breadth → ACTION(add_term) ──────────────────────────────
    ("at01", 'add "eta" to the math glossary', {}, ONLY_ACTION, "action", "add_term", None),
    ("at02", 'add "theta" to my biology word list', {}, ONLY_ACTION, "action", "add_term", None),
    ("at03", "将“kappa”录入数学词库", {}, ONLY_ACTION, "action", "add_term", None),
    ("at04", "把“lambda”加到编程单词库", {}, ONLY_ACTION, "action", "add_term", None),
    # ── pdf asset tool (context-determined asset) ─────────────────────────────────
    ("pa01", "extract the text from this document", {"owned": "a1"}, ONLY_ACTION, "action", "pdf_extract_text", None),
    ("pa02", "get the text of this file", {"attach": {"asset_id": "a2"}}, ONLY_ACTION, "action", "pdf_extract_text", None),
    ("pa03", "帮我把这篇文档提取全文", {"owned": "a3"}, ONLY_ACTION, "action", "pdf_extract_text", None),
    ("pa04", "把附件转换成文字", {"attach": {"asset_id": "a4"}}, ONLY_ACTION, "action", "pdf_extract_text", None),
    ("pa05", "extract the text from this document", {}, ONLY_ACTION, "agent", None, ANY),  # no asset ctx
    # ── abstain → Agent (recognition failure = lossless fallback) ──────────────────
    ("ab01", "新建文件夹没有名字", {}, ONLY_ACTION, "agent", None, ANY),
    ("ab02", 'rename the folder "x"', {}, ONLY_ACTION, "agent", None, ANY),
    ("ab03", 'delete the folder "x"', {}, ONLY_ACTION, "agent", None, ANY),
    ("ab04", 'add "y" to vocab', {}, ONLY_ACTION, "agent", None, ANY),  # empty domain
    ("ab05", 'create a folder named "x" and add "y" to my vocab', {}, ONLY_ACTION, "agent", None, ANY),
    ("ab06", 'make a folder', {}, ONLY_ACTION, "agent", None, ANY),
    # ── viewer content-kind matrix ────────────────────────────────────────────────
    ("vk01", "summarize this", {"viewer": _v("selection")}, FULL, "viewer", None, ANY),
    ("vk02", "what does this page say", {"viewer": _v("page")}, FULL, "viewer", None, ANY),
    ("vk03", "explain the window", {"viewer": _v("subtitle_window")}, FULL, "viewer", None, ANY),
    ("vk04", "main idea of full text", {"viewer": _v("full_text")}, FULL, "viewer", None, ANY),
    ("vk05", "over the subtitles", {"viewer": _v("full_subtitles")}, FULL, "viewer", None, ANY),
    ("vk06", "describe this region", {"viewer": _viewer(kinds=("roi",))}, FULL, "agent", None, ANY),
    ("vk07", "describe this frame", {"viewer": _viewer(kinds=("frame",))}, FULL, "agent", None, ANY),
    ("vk08", "what is shown", {"viewer": _viewer(kinds=("selection",), image=True)}, FULL, "agent", None, ANY),
    ("vk09", "summarize this", {"viewer": _v("full_text")}, dict(FULL, viewer=False), "agent", None, ANY),
    # ── viewer × other-capability combos ──────────────────────────────────────────
    ("vx01", "summarize this and the latest news", {"viewer": _v("full_text")}, FULL, "agent", None, ANY),
    ("vx02", "save this selection", {"viewer": _v("full_text")}, FULL, "agent", None, ANY),
    ("vx03", "compare this passage with my notes", {"viewer": _v("full_text")}, FULL, "composite", None, "private_first"),
    ("vx04", "compare this passage with my notes", {"viewer": _v("full_text")}, P5A, "agent", None, None),
    # ── private / attach / source-policy combinations ─────────────────────────────
    ("pr01", "what do my documents say about x", {}, P5A, "local_rag", None, "private_first"),
    ("pr02", "根据我们的知识库解释 y", {}, P5A, "local_rag", None, "private_first"),
    ("pr03", "summarize the attached file", {"attach": {"asset_id": "a"}}, FULL, "agent", None, ANY),
    ("sp01", "answer only from my knowledge base what x is", {}, FULL, "local_rag", None, "private_only"),
    ("sp02", "只用我的知识库回答什么是x", {}, FULL, "local_rag", None, "private_only"),
    ("sp03", "what do my notes say about x, no web", {}, FULL, "local_rag", None, "private_only"),
    ("sp04", "what do my notes say, if unsure you can search the web", {}, FULL, "local_rag", None, None),
    ("sp05", "compare my notes with the latest news", {}, FULL, "agent", None, None),
    ("sp06", "only from my knowledge base and the latest news", {}, FULL, "agent", None, None),
    # ── dynamic / multi-step / over-length → Agent ────────────────────────────────
    ("ag01", "first check the weather today then summarize my notes", {}, FULL, "agent", None, ANY),
    ("ag02", "outline a plan", {"research": True}, FULL, "agent", None, ANY),
    ("ag03", "continue the task", {"handoff": {"task": "t"}}, FULL, "agent", None, ANY),
    ("ag04", "and also make it shorter", {}, FULL, "direct", None, ANY),
]


@pytest.mark.parametrize("cid,msg,ctxf,gates,exp_kind,exp_tool,exp_sp", BOUNDARY,
                         ids=[r[0] for r in BOUNDARY])
def test_boundary_matrix(cid, msg, ctxf, gates, exp_kind, exp_tool, exp_sp):
    kind, plan, _ = route(msg, gates=gates, **ctxf)
    assert kind == exp_kind, cid
    if exp_tool is not None:
        assert plan.action["tool"] == exp_tool, cid
    if exp_sp is not ANY:
        assert plan.source_policy == exp_sp, cid


@pytest.mark.xfail(
    strict=True,
    reason=(
        "VALIDATED FINDING (same family as the DIRECT sequence gap; Phase-4 RAG): a "
        "sequenced chain whose SECOND capability has no lexical trigger — 'search the web' "
        "is invisible to _WEB_PAT (which only lists temporal words) — is absorbed into the "
        "single-capability LOCAL_RAG path, silently dropping the web step. _SEQUENCE_PAT "
        "exists to detect exactly this multi-hop pattern but is applied ONLY to COMPOSITE, "
        "never to LOCAL_RAG/DIRECT. Not a 5A regression; flagged for report. Contrast: "
        "when the web step uses a real _WEB_PAT word (weather/today/news) needs_web goes "
        "HIGH and the turn correctly falls to the Agent."
    ),
)
def test_rag_should_not_absorb_sequence_without_web_word():
    kind, _, _ = route("summarize my notes and then search the web for context")
    # The invariant we WANT: a sequenced {private + (untriggered) web} chain → Agent.
    assert kind == "agent"


# ════════════════════════════════════════════════════════════════════════════════
# EDGE — borderline Direct length/CJK + gate-permutation + context-flag rows
# (pins the explicitly-named charter edges: 中英 borderline, length threshold,
#  master-switch-off ⇒ everything Agent, context impurity ⇒ lossless Agent)
# ════════════════════════════════════════════════════════════════════════════════
_N = settings.chat_direct_max_chars

# (cid, msg, gates, ctx-kwargs, expected kind)
EDGE = [
    ("ed01", "z" * _N,                FULL, {},                      "direct"),
    ("ed02", "z" * (_N + 5),          FULL, {},                      "agent"),
    ("ed03", "z" * (_N - 1),          FULL, {},                      "direct"),
    ("ed04", "what is the weather",   FULL, {},                      "agent"),
    ("ed05", "好的，明白了，谢谢",      FULL, {},                      "direct"),
    ("ed06", "hello there",           DARK, {},                      "agent"),
    ("ed07", 'create a folder named "x"', DARK, {},                  "agent"),
    ("ed08", "hello there",           FULL, {"attach": {"asset_id": "a"}}, "agent"),
    ("ed09", 'create a folder named "x"', FULL, {"research": True},   "agent"),
    ("ed10", "what do my notes say about x", FULL, {"handoff": {"task": "t"}}, "agent"),
    ("ed11", "hello there",           ONLY_ACTION, {},               "agent"),
    ("ed12", "summarize this passage", {**FULL, "viewer": False}, {"viewer": _viewer()}, "agent"),
    ("ed13", "compare this passage with my notes", {**FULL, "composite": False}, {"viewer": _viewer()}, "agent"),
]


@pytest.mark.parametrize("cid,msg,gates,ctxf,exp", EDGE, ids=[r[0] for r in EDGE])
def test_edge_rows(cid, msg, gates, ctxf, exp):
    assert route(msg, gates=gates, **ctxf)[0] == exp, cid


# Private-only phrasing fused with a certified ACTION: the tool still dispatches,
# but the ORIGINAL policy keeps its private_only fence (never derived from a failure).
def test_edge_private_only_action_keeps_fence():
    kind, plan, reqs = route('answer only from my knowledge base, create a folder named "x"')
    assert kind == "action" and reqs.private_only is True
    assert plan.source_policy == "private_only"


