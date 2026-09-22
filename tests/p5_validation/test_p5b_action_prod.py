"""Layer B — ACTION dispatched through the REAL /chat/stream + sandbox funnel.

Unlike the Phase-5 golden tests (which substitute ``chat._run_tool``), these cases keep
``_run_tool`` REAL, so a certified ACTION traverses the exact
``ToolRuntime.execute`` waterfall the Agent would: pre-execute ASK → approval bridge →
monotonic sandbox/source-policy guards → the real tool body. That lets each assertion be
about a CAPABILITY OUTCOME (was the folder created? how many times? did the Agent run?)
rather than about a mocked return value.
"""
from __future__ import annotations

import pytest
from agent.security.sandbox import SandboxDecision
from agent.tools.tool_permissions import ToolPermission

from tests.p5_validation._p5_harness import (
    USER,
    FakeSeam,
    ScriptedPort,
    Spy,
    build_app,
    build_kernel,
    domains_named,
    sse,
)
from tests.p5_validation.test_p5_smoke import _gate

STEP = {"content": ["Agent took over."], "tool_calls": None}


# ── happy paths: tool body executed once, deterministic confirmation, Agent never runs ──
@pytest.mark.parametrize("msg,expected_name", [
    ('create a folder named "archive"', "archive"),
    ("创建文件夹“项目”", "项目"),
    ('make a new directory called "tmp"', "tmp"),
])
async def test_action_create_folder_executes_once(monkeypatch, msg, expected_name):
    port = ScriptedPort(steps=[STEP]); spy = Spy()
    kernel, _, _, broker = build_kernel(monkeypatch, port, spy, broker_mode="allow")
    _gate(monkeypatch, action=True)
    app = build_app(monkeypatch, port, FakeSeam([]), kernel, broker)
    res = await sse(app, msg)
    assert spy.folders_created == [(str(USER), expected_name)]
    assert f"Created folder '{expected_name}'" in res.answer
    assert port.steps == 0 and port.single_shot == 0      # no LLM, no Agent
    assert res.approvals                                   # WRITE ⇒ ASK surfaced + allowed


@pytest.mark.parametrize("msg,term,domain", [
    ('add "quantum" to my science vocab', "quantum", "Science"),
    ("把“熵”加入我的科学词汇库", "熵", "科学"),
])
async def test_action_add_term_executes_once(monkeypatch, msg, term, domain):
    port = ScriptedPort(steps=[STEP]); spy = Spy()
    # domains named case-insensitively; the ZH term resolves via strip().lower() match.
    kernel, _, _, broker = build_kernel(
        monkeypatch, port, spy, broker_mode="allow",
        domains=domains_named("Science", "科学"),
    )
    _gate(monkeypatch, action=True)
    app = build_app(monkeypatch, port, FakeSeam([]), kernel, broker)
    res = await sse(app, msg)
    assert len(spy.terms_added) == 1
    assert spy.terms_added[0][1] == term
    assert "Added" in res.answer or "词汇" in res.answer
    assert port.steps == 0


# ── permission funnel: ALLOW (WRITE granted) skips the ASK entirely ───────────────────
async def test_action_write_granted_no_approval(monkeypatch):
    port = ScriptedPort(steps=[STEP]); spy = Spy()
    kernel, _, _, broker = build_kernel(
        monkeypatch, port, spy, broker_mode="allow", grant=[ToolPermission.WRITE],
    )
    _gate(monkeypatch, action=True)
    app = build_app(monkeypatch, port, FakeSeam([]), kernel, broker)
    res = await sse(app, 'create a folder named "quiet"')
    assert res.approvals == []                             # granted ⇒ no ASK
    assert spy.folders_created == [(str(USER), "quiet")]


# ── DECIDED denials: pre-body, terminal, ZERO side effect, Agent NOT re-entered ────────
async def test_action_approval_denied_is_terminal_no_side_effect(monkeypatch):
    port = ScriptedPort(steps=[STEP]); spy = Spy()
    kernel, _, _, broker = build_kernel(monkeypatch, port, spy, broker_mode="deny")
    _gate(monkeypatch, action=True)
    app = build_app(monkeypatch, port, FakeSeam([]), kernel, broker)
    res = await sse(app, 'create a folder named "blocked"')
    assert spy.folders_created == []
    assert port.steps == 0                                 # escalation would re-prompt
    assert res.answer.startswith("Could not complete")     # decided-denial honesty


async def test_action_approval_timeout_is_terminal_no_side_effect(monkeypatch):
    port = ScriptedPort(steps=[STEP]); spy = Spy()
    kernel, _, _, broker = build_kernel(monkeypatch, port, spy, broker_mode="timeout")
    _gate(monkeypatch, action=True)
    app = build_app(monkeypatch, port, FakeSeam([]), kernel, broker)
    res = await sse(app, 'create a folder named "late"')
    assert spy.folders_created == []
    assert port.steps == 0
    assert res.answer.startswith("Could not complete")


async def test_action_source_policy_hard_deny_is_terminal(monkeypatch):
    """A DENY rule fences WRITE before the body ⇒ decided denial, no folder."""
    port = ScriptedPort(steps=[STEP]); spy = Spy()
    kernel, _, _, broker = build_kernel(
        monkeypatch, port, spy, broker_mode="allow",
        rules=[(ToolPermission.WRITE, SandboxDecision.DENY)],
    )
    _gate(monkeypatch, action=True)
    app = build_app(monkeypatch, port, FakeSeam([]), kernel, broker)
    res = await sse(app, 'create a folder named "denied"')
    assert spy.folders_created == []
    assert res.approvals == []                             # DENY short-circuits ASK
    assert res.answer.startswith("Could not complete")


# ── STATE UNKNOWN: body entered then failed ⇒ one write, NEVER a blind Agent retry ─────
async def test_action_state_unknown_never_replays(monkeypatch):
    port = ScriptedPort(steps=[STEP]); spy = Spy()
    kernel, _, _, broker = build_kernel(
        monkeypatch, port, spy, drive_mode="post-write", grant=[ToolPermission.WRITE],
    )
    _gate(monkeypatch, action=True)
    app = build_app(monkeypatch, port, FakeSeam([]), kernel, broker)
    res = await sse(app, 'create a folder named "maybe"')
    assert len(spy.folders_created) == 1                   # write happened, then broke
    assert port.steps == 0                                  # Agent NOT re-entered (no dup)
    assert "could not be confirmed" in res.answer.lower()


# ── LOSSLESS escalation: the Agent receives the ORIGINAL text, no notes, no pollution ──
async def test_action_preflight_escalates_byte_identical(monkeypatch):
    port = ScriptedPort(steps=[STEP]); spy = Spy()
    kernel, _, _, broker = build_kernel(
        monkeypatch, port, spy, drive_mode="preflight", grant=[ToolPermission.WRITE],
    )
    _gate(monkeypatch, action=True)
    app = build_app(monkeypatch, port, FakeSeam([]), kernel, broker)
    msg = 'create a folder named "rejected"'
    res = await sse(app, msg)
    assert spy.folders_created == []                        # DriveError is pre-write
    assert port.steps == 1                                    # Agent took the turn
    assert port.requests[-1][-1]["content"] == msg            # byte-for-byte original text
    assert "Private retrieval note" not in port.requests[-1][-1]["content"]
    assert res.answer == "Agent took over."


# ── schema gate (executor, before the seam): malformed ⇒ escalate, never execute ───────
async def test_action_oversized_slot_escalates_before_side_effect(monkeypatch):
    port = ScriptedPort(steps=[STEP]); spy = Spy()
    kernel, _, _, broker = build_kernel(
        monkeypatch, port, spy, broker_mode="allow",
    )
    _gate(monkeypatch, action=True)
    app = build_app(monkeypatch, port, FakeSeam([]), kernel, broker)
    big = "x" * 130                                          # > name max 120
    await sse(app, f'create a folder named "{big}"')
    assert spy.folders_created == []                         # schema gate precedes the body
    assert port.steps == 1                                    # Agent owns the clarification


# ── domain resolution: 0-match and >1-match are preflight ⇒ escalate, never write ──────
async def test_action_add_term_unknown_domain_escalates(monkeypatch):
    port = ScriptedPort(steps=[STEP]); spy = Spy()
    kernel, _, _, broker = build_kernel(
        monkeypatch, port, spy, broker_mode="allow", domains=domains_named("Science"),
    )
    _gate(monkeypatch, action=True)
    app = build_app(monkeypatch, port, FakeSeam([]), kernel, broker)
    res = await sse(app, 'add "x" to my nonexistent vocab')
    assert spy.terms_added == []
    assert port.steps == 1                                    # preflight ⇒ Agent clarifies
    assert res.answer == "Agent took over."


async def test_action_add_term_ambiguous_domain_escalates(monkeypatch):
    port = ScriptedPort(steps=[STEP]); spy = Spy()
    kernel, _, _, broker = build_kernel(
        monkeypatch, port, spy, broker_mode="allow",
        domains=domains_named("Music", "Music"),            # two visible domains, same name
    )
    _gate(monkeypatch, action=True)
    app = build_app(monkeypatch, port, FakeSeam([]), kernel, broker)
    res = await sse(app, 'add "key" to my music vocab')
    assert spy.terms_added == []
    assert port.steps == 1                                    # NEVER pick matches[0]
    assert res.answer == "Agent took over."


# ── Tool Registry is the SOLE capability source: a certified-but-unregistered tool
#    falls back losslessly (the fast path never invents a capability). ──────────────────
async def test_action_unregistered_certified_tool_falls_back(monkeypatch):
    # pdf_extract_text is in the DIRECT_TOOLS allowlist (so it CAN certify) but is NOT
    # registered in this kernel's runtime — dispatch must escalate, not error.
    port = ScriptedPort(steps=[STEP]); spy = Spy()
    kernel, _, _, broker = build_kernel(monkeypatch, port, spy, broker_mode="allow")
    _gate(monkeypatch, action=True)
    app = build_app(monkeypatch, port, FakeSeam([]), kernel, broker)
    # A create_folder phrase that the extractor certifies, then we strip the tool:
    del kernel.runtime._tools["create_folder"]
    res = await sse(app, 'create a folder named "ghost"')
    assert spy.folders_created == []
    assert port.steps == 1                                    # unknown_tool ⇒ preflight ⇒ Agent
    assert res.answer == "Agent took over."
