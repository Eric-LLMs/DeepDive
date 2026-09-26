"""P1 step 3 — the Matcher node (live-table exact corpus only, zero business
rules) and its coexistence shadow wiring in funnel.route.

Two things these tests pin harder than anything else in P1:
* AMBIGUOUS carries ALL candidates and the node NEVER picks (8.1);
* the shadow hook cannot change the turn — with a broken registry read, with
  the switch off, or mid-cascade, ``route`` still returns the byte-identical
  requirements it would have pre-step-3 (P0 behavior compatibility).
"""
from __future__ import annotations

import logging
import types

import pytest
from core.application.chat.intent_funnel import funnel, matcher
from core.application.chat.intent_funnel.contract import (
    MATCH_AMBIGUOUS,
    MATCH_HIT,
    MATCH_MISS,
    TurnFacts,
)
from core.application.chat.intent_funnel.registry import content_fingerprint
from core.application.chat.intent_funnel.registry import entry as T
from core.application.chat.intent_funnel.registry.entry import (
    RegistryLiveView,
    derive_language,
)
from core.application.chat.understanding import (
    Complexity,
    Confidence,
    Signal,
    TurnRequirements,
)


def _q(i, text, *, position=0, standard_query_id=None):
    return T.QueryRecord(id=f"q{i}", query=text, language=derive_language(text),
                         position=position, standard_query_id=standard_query_id)


def _entry(cid, *, tool="create_folder", standard="", synonyms=(), patterns=(),
           aliases=(), enabled=True, status="active", examples=("做个事",)):
    std = [_q(1, standard)] if standard else []
    sims = [_q(10 + n, s, standard_query_id="q1" if std else None)
            for n, s in enumerate(synonyms)]
    return T.CapabilityEntry(
        capability_id=cid, tool_binding=tool, description="d",
        standard_queries=tuple(std), similar_queries=tuple(sims),
        patterns=tuple(patterns), aliases=tuple(aliases),
        request_query_examples=tuple(examples),
        enabled=enabled, status=status,
    )


def _view(entries, fingerprint=None):
    return RegistryLiveView(
        # content-derived like production: distinct entry sets NEVER share the
        # cache key (a constant fake here would poison the index cache cross-test)
        fingerprint=fingerprint or content_fingerprint(list(entries)),
        entries=tuple(entries),
    )


# ── node semantics (Exact Positive Match only, ruling 2026-09-25) ────────────────

_TF = TurnFacts()  # plain turn: no viewer/attachment facts


def test_exact_standard_hit_is_normalized():
    v = _view([_entry("cap-a", standard=" 新建文件夹 ")])
    res = matcher.match("新建文件夹", _TF, v)
    assert res.state == MATCH_HIT and res.capability_id == "cap-a"
    assert res.registry_version == v.fingerprint
    # the HIT literal is the normalized corpus sentence — never a regex string
    assert res.matched_literal == "新建文件夹"


def test_exact_synonym_hit():
    v = _view([_entry("cap-a", standard="新建文件夹", synonyms=("建个新目录",))])
    res = matcher.match("建个新目录", _TF, v)
    assert res.state == MATCH_HIT and res.matched_literal == "建个新目录"


def test_regex_patterns_and_aliases_are_inert_storage():
    # The Action-Contract ruling deletes regex from the Matcher's runtime path:
    # a stored ``re:`` pattern or a legacy alias can NEVER produce a HIT, even
    # as an exact whole-sentence match.
    v = _view([_entry("cap-a", patterns=("re:新建文件夹", "新建文件夹"),
                      aliases=("建目录",))])
    assert matcher.match("新建文件夹", _TF, v).state == MATCH_MISS
    assert matcher.match("帮我新建文件夹好吗", _TF, v).state == MATCH_MISS
    assert matcher.match("建目录", _TF, v).state == MATCH_MISS


def test_request_examples_are_not_match_data():
    v = _view([_entry("cap-a", standard="新建文件夹",
                      examples=("请创建一个文件夹",))])
    assert matcher.match("请创建一个文件夹", _TF, v).state == MATCH_MISS


@pytest.mark.parametrize("query", [
    "你能不能创建文件夹？",   # question about capability
    "怎么创建文件夹？",       # how-to
    "不要创建文件夹",         # negation
    "如果需要创建文件夹，告诉我",  # hypothetical
    '我同事说"创建一个文件夹"',    # quoted speech
    "创建一个叫 notes 的文件夹",   # new phrasing (may go recall+model)
    "请创建 notes 文件夹",         # ditto
])
def test_sentence_variants_never_hit_the_exact_table(query):
    # corpus holds the canonical sentences; every non-identical sentence above
    # must MISS (escalate to Recall + ToolIntentModel), and NONE of them may be
    # substring-matched the way the old regex lane did.
    v = _view([_entry("cap-folder", standard="创建一个文件夹",
                      synonyms=("请新建一个文件夹",))])
    assert matcher.match(query, _TF, v).state == MATCH_MISS


def test_disabled_and_deprecated_caps_are_never_matched():
    v = _view([
        _entry("cap-off", standard="建目录", enabled=False, status="disabled"),
        _entry("cap-dep", standard="建个目录", status="deprecated"),
    ])
    assert matcher.match("建目录", _TF, v).state == MATCH_MISS
    assert matcher.match("建个目录", _TF, v).state == MATCH_MISS


def test_disabled_query_rows_are_not_match_data():
    # row-level enablement lives in intent_corpus (enabled-only): a disabled
    # Similar row must drop out of the exact table.
    e = T.CapabilityEntry(
        capability_id="cap-a", tool_binding="create_folder",
        standard_queries=(_q(1, "新建文件夹"),),
        similar_queries=(T.QueryRecord(id="q2", query="建个目录",
                                       language="zh", enabled=False),),
    )
    v = _view([e])
    assert matcher.match("建个目录", _TF, v).state == MATCH_MISS
    assert matcher.match("新建文件夹", _TF, v).state == MATCH_HIT


def test_ambiguous_carries_all_candidates_and_never_picks():
    # the SAME curated sentence registered under two capabilities — exact-only
    # AMBIGUOUS can only come from human curation overlap, never from regex.
    v = _view([
        _entry("cap-a", standard="季度汇总"),
        _entry("cap-b", synonyms=("季度汇总",)),
    ])
    res = matcher.match("季度汇总", _TF, v)
    assert res.state == MATCH_AMBIGUOUS
    assert res.capability_id is None
    assert set(res.candidates) == {"cap-a", "cap-b"}  # ALL of them, upward


def test_blank_query_misses_and_bad_legacy_regex_is_skipped_not_fatal():
    v = _view([_entry("cap-a", patterns=("re:[unclosed",), standard="x")])
    assert matcher.match("", _TF, v).state == MATCH_MISS
    assert matcher.match("x", _TF, v).state == MATCH_HIT  # the corpus row still works


def test_index_is_cached_per_content_fingerprint():
    v = _view([_entry("cap-a", standard="x")])
    matcher._INDEX_CACHE.clear()
    first = matcher.build_index(v)
    assert matcher.build_index(v) is first  # same fingerprint -> same object
    v2 = _view([_entry("cap-a", standard="x", synonyms=("y",))])
    assert matcher.build_index(v2) is not first  # different content -> new index


# ── shadow wiring: observation, never behavior ───────────────────────────────────

def _ctx(msg):
    return types.SimpleNamespace(body=types.SimpleNamespace(message=msg),
                                 owned_asset_id=None)


def _req():
    return TurnRequirements(complexity=Complexity.LOW, confidence=Confidence.LOW,
                            needs_web=Signal.LOW, needs_memory=False)


async def test_shadow_hook_is_inert_when_the_mode_switch_is_off(monkeypatch):
    from core.config import settings

    monkeypatch.setattr(settings, "chat_matcher_mode", "off")
    calls = []
    monkeypatch.setattr(
        "core.application.chat.intent_funnel.registry.active_view",
        lambda **kw: calls.append(1),  # would raise if awaited; never called
    )
    req = _req()
    out = await funnel.route(_ctx("新建文件夹"), deps=object(), requirements=req)
    assert calls == [] and out is req  # exactly as pre-step-3


async def test_shadow_logs_would_verdict_without_touching_routing(monkeypatch, caplog):
    from core.config import settings

    monkeypatch.setattr(settings, "chat_matcher_mode", "shadow")
    view = _view([_entry("cap-a", standard="新建文件夹")])

    async def fake_active(**kw):
        return view

    monkeypatch.setattr(
        "core.application.chat.intent_funnel.registry.active_view", fake_active
    )
    req = _req()
    with caplog.at_level(logging.INFO, logger="core.application.chat.intent_funnel"):
        # deps without session_factory/embedder: the shadow read may work but the
        # live cascade must fail-open — both outcomes must return THE SAME object.
        out = await funnel.route(
            _ctx("新建文件夹"), deps=types.SimpleNamespace(session_factory=None),
            requirements=req,
        )
    assert out is req  # byte-identical turn: the shadow cannot certify an action
    line = next(r.getMessage() for r in caplog.records if "matcher_shadow" in r.getMessage())
    assert "state=HIT" in line and "would_capability=cap-a" in line
    # the 8.15 telemetry vocabulary (live-table ruling: fingerprint, no version)
    assert "would_route=t" in line and "would_stage=matcher" in line
    assert "confidence=1.0" in line and "fallback_reason=-" in line
    assert f"registry_fingerprint={view.fingerprint}" in line
    assert "pattern=新建文件夹" in line  # which exact corpus sentence produced the HIT
    assert "agreement=matcher_only" in line  # Matcher hit, L0 abstained


async def test_shadow_read_failure_is_fail_quiet(monkeypatch, caplog):
    from core.config import settings

    monkeypatch.setattr(settings, "chat_matcher_mode", "shadow")

    async def boom(**kw):
        raise RuntimeError("registry store down")

    monkeypatch.setattr(
        "core.application.chat.intent_funnel.registry.active_view", boom
    )
    req = _req()
    with caplog.at_level(logging.INFO):
        out = await funnel.route(
            _ctx("hello"), deps=types.SimpleNamespace(session_factory=None),
            requirements=req,
        )
    assert out is req
    assert any("fail-quiet" in r.getMessage() for r in caplog.records)


async def test_shadow_sees_l0_certification_for_comparison(monkeypatch, caplog):
    """The log line carries BOTH sides: L0's tool and the Matcher verdict — that
    pairing is the step-3 equivalence dataset."""
    from core.config import settings

    monkeypatch.setattr(settings, "chat_matcher_mode", "shadow")
    view = _view([_entry("cap-a", tool="create_folder", standard="随便")])

    async def fake_active(**kw):
        return view

    monkeypatch.setattr(
        "core.application.chat.intent_funnel.registry.active_view", fake_active
    )
    req = TurnRequirements(
        complexity=Complexity.LOW, confidence=Confidence.LOW,
        needs_web=Signal.LOW, needs_memory=False,
        requested_action={"tool": "create_folder", "args": {}},
    )
    with caplog.at_level(logging.INFO, logger="core.application.chat.intent_funnel"):
        out = await funnel.route(
            _ctx("提个要求"), deps=types.SimpleNamespace(session_factory=None),
            requirements=req,
        )
    assert out is req  # L0-certified turns are untouched by the shadow, too
    line = next(r.getMessage() for r in caplog.records if "matcher_shadow" in r.getMessage())
    assert "l0_tool=create_folder" in line and "state=MISS" in line
    assert "would_route=f" in line and "fallback_reason=matcher_miss" in line
    assert "agreement=l0_only" in line  # L0 certified, Matcher abstained


async def test_shadow_agreement_match_and_mismatch(monkeypatch, caplog):
    """The two adjudication samples P2 promotion needs: HIT agreeing with L0,
    and HIT naming a different tool than L0 certified."""
    from core.config import settings

    monkeypatch.setattr(settings, "chat_matcher_mode", "shadow")

    async def fake_active(**kw):
        return _view([_entry("cap-a", tool="create_folder", standard="新建文件夹")])

    monkeypatch.setattr(
        "core.application.chat.intent_funnel.registry.active_view", fake_active
    )
    for l0, expect in (("create_folder", "agreement=match"), ("add_term", "agreement=mismatch")):
        req = TurnRequirements(
            complexity=Complexity.LOW, confidence=Confidence.LOW,
            needs_web=Signal.LOW, needs_memory=False,
            requested_action={"tool": l0, "args": {}},
        )
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="core.application.chat.intent_funnel"):
            out = await funnel.route(
                _ctx("新建文件夹"), deps=types.SimpleNamespace(session_factory=None),
                requirements=req,
            )
        assert out is req  # agreement or not, the shadow never touches the turn
        line = next(r.getMessage() for r in caplog.records if "matcher_shadow" in r.getMessage())
        assert expect in line and "state=HIT" in line


# ── step 5: the tri-state switch + the 8.14 cost pin ─────────────────────────────

async def test_mode_on_runs_shadow_semantics_with_a_warning(monkeypatch, caplog):
    """ON is the P2 promotion: with the funnel gate closed it must NEVER route —
    it runs shadow and warns once so a mis-set switch is loud in the logs but
    inert in behavior."""
    from core.application.chat.intent_funnel import shadow
    from core.config import settings

    monkeypatch.setattr(shadow, "_warned_on", False)
    monkeypatch.setattr(settings, "chat_funnel_enabled", False)
    monkeypatch.setattr(settings, "chat_matcher_mode", "on")
    view = _view([_entry("cap-a", standard="新建文件夹")])

    async def fake_active(**kw):
        return view

    monkeypatch.setattr(
        "core.application.chat.intent_funnel.registry.active_view", fake_active
    )
    req = _req()
    with caplog.at_level(logging.INFO, logger="core.application.chat.intent_funnel"):
        out = await funnel.route(
            _ctx("新建文件夹"), deps=types.SimpleNamespace(session_factory=None),
            requirements=req,
        )
    assert out is req  # ON does not hand the HIT to the turn — the funnel gate is off
    assert any("chat_funnel_enabled is off" in r.getMessage() for r in caplog.records)
    assert any("matcher_shadow mode=on" in r.getMessage() for r in caplog.records)


async def test_unknown_mode_fails_safe_to_off(monkeypatch):
    from core.config import settings

    monkeypatch.setattr(settings, "chat_matcher_mode", "SHADOWW")  # typo
    calls = []
    monkeypatch.setattr(
        "core.application.chat.intent_funnel.registry.active_view",
        lambda **kw: calls.append(1),
    )
    req = _req()
    out = await funnel.route(_ctx("新建文件夹"), deps=object(), requirements=req)
    assert calls == [] and out is req


async def test_shadow_pins_execution_mode_and_always_releases_it(monkeypatch):
    """8.14: the observation runs under execution_mode=shadow — any usage a
    future shadow stage logs must be unbilled. The pin never leaks out, even
    when the observation itself raises."""
    from core.application.chat.intent_funnel.shadow import observe
    from core.infrastructure.request_context import (
        get_request_execution_mode,
    )

    seen = {}

    async def spy_active(**kw):
        seen["inside"] = get_request_execution_mode()
        raise RuntimeError("registry store down")

    monkeypatch.setattr(
        "core.application.chat.intent_funnel.registry.active_view", spy_active
    )
    await observe(
        _ctx("hi"), types.SimpleNamespace(session_factory=None), _req(), "shadow",
    )
    assert seen["inside"] == "shadow"
    assert get_request_execution_mode() == "production"  # fail-quiet also unwinds
