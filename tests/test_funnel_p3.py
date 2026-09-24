"""P3 — intent_kind: the table widened, the gates did not.

Pinning the P3 ruling ("扩表不改接口,逐开关灰度"):

* the kind is Registry DATA: it rides the draft row, the published payload and
  the content fingerprint (changing a kind changes the version);
* pre-P3 payloads and rows default to ACTION — an old active version can never
  route a widened kind;
* an enabled-looking verdict on a CLOSED kind exits with FUNNEL_KIND_DISABLED
  and the Agent turn stays byte-identical (8.10);
* the interface: nothing outside the Registry changed — the cascade, the binder
  and the executor contract are P2 shapes, untouched.
"""
from __future__ import annotations

import logging
import re
import types

from core.application.chat.intent_funnel import funnel
from core.application.chat.intent_funnel.contract import REASON_KIND_DISABLED
from core.application.chat.intent_funnel.registry import content_fingerprint
from core.application.chat.intent_funnel.registry import snapshot as pub
from core.application.chat.intent_funnel.registry.entry import (
    KIND_ACTION,
    KIND_PRIVATE,
    KIND_WEB,
    CapabilityEntry,
    RegistryVersionView,
)

MSG = '新建文件夹"季度报告"'


def _entry(cid, *, tool="create_folder", aliases=(), kind=KIND_ACTION, **kw):
    return CapabilityEntry(
        capability_id=cid, tool_binding=tool, description=f"does {cid}",
        aliases=tuple(aliases), examples=("做个事",),
        parameters={"name": {"type": "string", "required": True,
                             "max_len": 120, "description": "folder name"}},
        arg_slots={"name": {"source": "user_input"}}, intent_kind=kind, **kw,
    )


def _view(entries, version=1):
    return RegistryVersionView(
        version=version, state="active",
        fingerprint=content_fingerprint(list(entries)),
        capabilities=(), entries=tuple(entries),
    )


# ════════════════════════ data: the kind round-trips everywhere ═════════════════


def test_kind_round_trips_through_payload_and_defaults_to_action():
    e = _entry("cap-p", kind=KIND_PRIVATE)
    assert CapabilityEntry.from_payload(e.to_payload()).intent_kind == KIND_PRIVATE
    # a pre-P3 published payload has no kind field at all -> ACTION, the safe
    # historical default (an old version must not route a widened kind)
    legacy = {k: v for k, v in e.to_payload().items() if k != "intent_kind"}
    assert CapabilityEntry.from_payload(legacy).intent_kind == KIND_ACTION


def test_kind_is_content_so_it_moves_the_fingerprint():
    a = _view([_entry("cap-a", kind=KIND_ACTION)])
    p = _view([_entry("cap-a", kind=KIND_PRIVATE)])
    assert a.fingerprint != p.fingerprint


def test_publish_gate_rejects_unknown_kind():
    issues = pub.validate_entries([_entry("cap-x", kind="quantum")])
    assert any("intent_kind" in s for s in issues)
    # the three real kinds pass the kind rule (other rules may still apply)
    assert not [s for s in pub.validate_entries([_entry("cap-x", kind=KIND_WEB)])
                if "intent_kind" in s]


def test_kind_enabled_matrix():
    from core.config import settings

    assert funnel.kind_enabled(KIND_ACTION)          # rides the master gate
    assert funnel.kind_enabled("")                   # historical rows: action
    assert not funnel.kind_enabled("quantum")        # unknown kind routes nothing
    saved_p, saved_w = settings.chat_funnel_private_enabled, settings.chat_funnel_web_enabled
    try:
        settings.chat_funnel_private_enabled = False
        settings.chat_funnel_web_enabled = False
        assert not funnel.kind_enabled(KIND_PRIVATE)
        assert not funnel.kind_enabled(KIND_WEB)
        settings.chat_funnel_private_enabled = True
        assert funnel.kind_enabled(KIND_PRIVATE)
    finally:
        settings.chat_funnel_private_enabled = saved_p
        settings.chat_funnel_web_enabled = saved_w


# ════════════════════════ cascade: in the table != ON (8.10 exit) ═══════════════


def _ctx(msg):
    return types.SimpleNamespace(
        body=types.SimpleNamespace(message=msg, attach=None, viewer=None),
        owned_asset_id=None, research_turn=False, effective_handoff=None,
        session_id="s-1",
    )


def _req():
    from core.application.chat.understanding import (
        Complexity, Confidence, Signal, TurnRequirements,
    )
    return TurnRequirements(complexity=Complexity.LOW, confidence=Confidence.LOW,
                            needs_web=Signal.LOW, needs_memory=False)


class _Embedder:
    async def embed(self, texts):
        return [[1.0, 0.0] for _ in texts]


def _open(monkeypatch, *, mode="on", private=False, judge="online"):
    from core.config import settings

    monkeypatch.setattr(settings, "chat_funnel_enabled", True)
    monkeypatch.setattr(settings, "chat_fast_paths_enabled", True)
    monkeypatch.setattr(settings, "chat_action_fast_path_enabled", True)
    monkeypatch.setattr(settings, "chat_matcher_mode", mode)
    monkeypatch.setattr(settings, "chat_judge_backend", judge)
    monkeypatch.setattr(settings, "chat_judge_local_url", "")
    monkeypatch.setattr(settings, "chat_judge_min_confidence", 0.75)
    monkeypatch.setattr(settings, "chat_judge_online_model", "")
    monkeypatch.setattr(settings, "chat_judge_timeout_seconds", 5.0)
    monkeypatch.setattr(settings, "chat_funnel_timeout_seconds", 5.0)
    monkeypatch.setattr(settings, "chat_funnel_private_enabled", private)


class _ScriptedModelA:
    """Deterministic Model A double for the kind-gate lanes (the stub has no
    extraction power by design, so certification tests ride the online seam):
    one card -> select it and quote-strip the name; a split card set -> NONE."""

    def __init__(self):
        self.calls = 0

    async def complete_json(self, prompt, **kw):
        self.calls += 1
        caps = re.findall(r"(?m)^### (\S+)$", prompt)
        if len(caps) != 1:
            return {"capability_id": "NONE", "confidence": 1.0, "arguments": {}}
        m = re.search(r"<user_sentence>(.*?)</user_sentence>", prompt, re.DOTALL)
        quoted = re.search(r'"([^"]+)"', m.group(1) if m else "")
        args = {"name": quoted.group(1)} if quoted else {}
        return {"capability_id": caps[0], "confidence": 0.95, "arguments": args}


def _wire(monkeypatch, *, view, llm=None):
    async def fake_active(**kw):
        return view

    async def fake_load(sf):
        return types.SimpleNamespace(version="idx-9", capabilities=[],
                                     example_vectors=[])

    monkeypatch.setattr(
        "core.application.chat.intent_funnel.registry.active_view", fake_active)
    monkeypatch.setattr(
        "core.application.chat.intent_funnel.recall.load_index", fake_load)
    return types.SimpleNamespace(session_factory=None,
                                 embedder=lambda: _Embedder(),
                                 llm=llm if llm is not None else _ScriptedModelA())


async def test_closed_private_kind_exits_with_reason_byte_identical(monkeypatch, caplog):
    _open(monkeypatch, private=False)
    view = _view([_entry("cap-p", kind=KIND_PRIVATE, aliases=(MSG,))])
    deps = _wire(monkeypatch, view=view)
    req = _req()
    with caplog.at_level(logging.INFO, logger="core.application.chat.intent_funnel"):
        out = await funnel.route(_ctx(MSG), deps=deps, requirements=req)
    assert out is req                      # Agent keeps the turn (8.10)
    line = next(r.getMessage() for r in caplog.records if "funnel_trace" in r.getMessage())
    assert f"fallback_reason={REASON_KIND_DISABLED}" in line


async def test_opened_private_kind_certifies_with_kind_metadata(monkeypatch, caplog):
    _open(monkeypatch, private=True)
    view = _view([_entry("cap-p", kind=KIND_PRIVATE, aliases=(MSG,))])
    deps = _wire(monkeypatch, view=view)
    with caplog.at_level(logging.INFO, logger="core.application.chat.intent_funnel"):
        out = await funnel.route(_ctx(MSG), deps=deps, requirements=_req())
    act = out.requested_action
    assert act["funnel_kind"] == KIND_PRIVATE
    assert act["capability_id"] == "cap-p" and act["args"] == {"name": "季度报告"}
    line = next(r.getMessage() for r in caplog.records if "funnel_trace" in r.getMessage())
    assert "final_route=action" in line


async def test_action_kind_needs_no_extra_switch(monkeypatch):
    _open(monkeypatch, private=False)
    view = _view([_entry("cap-a", kind=KIND_ACTION, aliases=(MSG,))])
    deps = _wire(monkeypatch, view=view)
    out = await funnel.route(_ctx(MSG), deps=deps, requirements=_req())
    assert out.requested_action["funnel_kind"] == KIND_ACTION
