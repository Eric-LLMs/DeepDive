"""P1 step 3 — the Matcher node (table-only, zero business rules) and its
coexistence shadow wiring in funnel.route.

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
from core.application.chat import qir  # noqa: F401 - import order sanity for seams
from core.application.chat.intent_funnel import funnel, matcher
from core.application.chat.intent_funnel.contract import (
    MATCH_AMBIGUOUS,
    MATCH_HIT,
    MATCH_MISS,
)
from core.application.chat.intent_funnel.registry import content_fingerprint
from core.application.chat.intent_funnel.registry import types as T
from core.application.chat.intent_funnel.registry.types import RegistryVersionView
from core.application.chat.understanding import (
    Complexity,
    Confidence,
    Signal,
    TurnRequirements,
)


def _entry(cid, *, tool="create_folder", patterns=(), aliases=(), enabled=True,
           status="active", examples=("做个事",)):
    return T.CapabilityEntry(
        capability_id=cid, tool_binding=tool, description="d",
        patterns=tuple(patterns), aliases=tuple(aliases), examples=tuple(examples),
        enabled=enabled, status=status,
    )


def _view(entries, version=1, fingerprint=None):
    return RegistryVersionView(
        version=version, state="active",
        # content-derived like production: distinct entry sets NEVER share the
        # cache key (a constant fake here would poison the index cache cross-test)
        fingerprint=fingerprint or content_fingerprint(list(entries)),
        capabilities=(), entries=entries,
    )


# ── node semantics ────────────────────────────────────────────────────────────────

def test_exact_phrase_hit_is_normalized():
    v = _view([_entry("cap-a", aliases=(" 新建文件夹 ",))])
    res = matcher.match("新建文件夹", v)
    assert res.state == MATCH_HIT and res.capability_id == "cap-a"
    assert res.registry_version == v.fingerprint


def test_regex_pattern_hits_and_misses():
    v = _view([_entry("cap-a", patterns=(f"{T.RE_PREFIX}(?:创建|新建)文件夹",))])
    assert matcher.match("帮我新建文件夹好吗", v).capability_id == "cap-a"
    assert matcher.match("删除文件夹", v).state == MATCH_MISS


def test_disabled_and_deprecated_caps_are_never_matched():
    v = _view([
        _entry("cap-off", aliases=("建目录",), enabled=False, status="disabled"),
        _entry("cap-dep", aliases=("建个目录",), status="deprecated"),
    ])
    assert matcher.match("建目录", v).state == MATCH_MISS
    assert matcher.match("建个目录", v).state == MATCH_MISS


def test_ambiguous_carries_all_candidates_and_never_picks():
    v = _view([
        _entry("cap-a", aliases=("季度汇总",)),
        _entry("cap-b", patterns=(f"{T.RE_PREFIX}季度.*",)),
    ])
    res = matcher.match("季度汇总", v)
    assert res.state == MATCH_AMBIGUOUS
    assert res.capability_id is None
    assert set(res.candidates) == {"cap-a", "cap-b"}  # ALL of them, upward


def test_blank_query_misses_and_bad_legacy_regex_is_skipped_not_fatal():
    v = _view([_entry("cap-a", patterns=("re:[unclosed",), aliases=("x",))])
    assert matcher.match("", v).state == MATCH_MISS
    assert matcher.match("x", v).state == MATCH_HIT  # the other literal still works


def test_index_is_cached_per_version_fingerprint_pair():
    v = _view([_entry("cap-a", aliases=("x",))], version=1)
    matcher._INDEX_CACHE.clear()
    first = matcher.build_index(v)
    assert matcher.build_index(v) is first  # same (version, fingerprint) -> same object
    v2 = _view([_entry("cap-a", aliases=("x", "y"))], version=2)
    assert matcher.build_index(v2) is not first  # different content -> new index


# ── shadow wiring: observation, never behavior ───────────────────────────────────

def _ctx(msg):
    return types.SimpleNamespace(body=types.SimpleNamespace(message=msg),
                                 owned_asset_id=None)


def _req():
    return TurnRequirements(complexity=Complexity.LOW, confidence=Confidence.LOW,
                            needs_web=Signal.LOW, needs_memory=False)


async def test_shadow_hook_is_inert_when_the_funnel_switch_is_off(monkeypatch):
    from core.config import settings

    settings.chat_qir_enabled = False
    calls = []
    monkeypatch.setattr(
        "core.application.chat.intent_funnel.registry.active_view",
        lambda **kw: calls.append(1),  # would raise if awaited; never called
    )
    out = await funnel.route(_ctx("新建文件夹"), deps=object(), requirements=_req())
    assert calls == []  # physically dark, exactly as pre-step-3


async def test_shadow_logs_verdict_without_touching_routing(monkeypatch, caplog):
    from core.config import settings

    settings.chat_qir_enabled = True
    view = _view([_entry("cap-a", aliases=("新建文件夹",))])

    async def fake_active(**kw):
        return view

    monkeypatch.setattr(
        "core.application.chat.intent_funnel.registry.active_view", fake_active
    )
    req = _req()
    with caplog.at_level(logging.INFO, logger="core.application.chat.intent_funnel.funnel"):
        # deps without session_factory/embedder: the shadow read may work but the
        # live cascade must fail-open — both outcomes must return THE SAME object.
        out = await funnel.route(
            _ctx("新建文件夹"), deps=types.SimpleNamespace(session_factory=None),
            requirements=req,
        )
    assert out is req  # byte-identical turn: the shadow cannot certify an action
    lines = [r.getMessage() for r in caplog.records if "matcher_shadow" in r.getMessage()]
    assert lines and "state=HIT" in lines[0] and "cap=cap-a" in lines[0]


async def test_shadow_read_failure_is_fail_quiet(monkeypatch, caplog):
    from core.config import settings

    settings.chat_qir_enabled = True

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

    settings.chat_qir_enabled = True
    view = _view([_entry("cap-a", tool="create_folder", aliases=("随便",))])

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
    with caplog.at_level(logging.INFO, logger="core.application.chat.intent_funnel.funnel"):
        out = await funnel.route(
            _ctx("提个要求"), deps=types.SimpleNamespace(session_factory=None),
            requirements=req,
        )
    assert out is req  # L0-certified turns are untouched by the shadow, too
    line = next(r.getMessage() for r in caplog.records if "matcher_shadow" in r.getMessage())
    assert "l0_tool=create_folder" in line and "state=MISS" in line
