"""Shell-client & per-job pinning enforcement (dispatch-gateway v2).

Covers the four hard edges the slides-402 incident demanded:
1. ``OpenAILLM(require_channel=True)`` holds no usable key — a call without a
   gateway-resolved channel raises ``NoActiveChannelError`` (no global-key fallback);
2. per-field channel priority: explicit args > request ContextVar > client config;
3. ``_run`` pins the owner's channel at job start and ALWAYS resets it (a reused
   coroutine can never observe another job's credentials); a no-owner job pins nothing;
4. the toolkit enqueue gate authorizes through quota + gateway BEFORE any job row and
   the payload carries attribution only — never a plaintext key, never a base_url
   override (SSRF);
5. the static whitelist guard: every ``OpenAILLM(`` construction in production code is
   either an explicit shell or an allowlisted diagnostic — a new resident-key client
   fails CI.
"""
from __future__ import annotations

import asyncio
import json
import re
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from core.infrastructure import llm as llm_mod
from core.infrastructure.llm import NoActiveChannelError, OpenAILLM
from core.infrastructure.request_context import (
    get_request_llm_channel,
    reset_request_llm_channel,
    set_request_llm_channel,
)

REPO = Path(__file__).resolve().parents[1]


class _FakeClient:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


@pytest.fixture
def clients(monkeypatch):
    """Capture every AsyncOpenAI the module constructs (kwarg inspection, no network)."""
    made: list[_FakeClient] = []

    def factory(**kwargs):
        c = _FakeClient(**kwargs)
        made.append(c)
        return c

    monkeypatch.setattr(llm_mod, "AsyncOpenAI", factory)
    return made


# -- 1/2: shell posture & channel priority ----------------------------------------------

def test_shell_client_raises_without_a_pinned_channel(clients):
    shell = OpenAILLM(require_channel=True)
    with pytest.raises(NoActiveChannelError):
        shell._call_channel()


async def test_shell_client_explain_term_raises_too(clients):
    # explain_term used to bypass the channel logic entirely — same guard now.
    shell = OpenAILLM(require_channel=True)
    with pytest.raises(NoActiveChannelError):
        await shell.explain_term("procrastinate", "")


def test_contextvar_channel_beats_client_config(clients):
    shell = OpenAILLM(require_channel=True)
    tok = set_request_llm_channel(("pinned-model", "http://gw", "sk-pin"))
    try:
        client, mdl = shell._call_channel()
    finally:
        reset_request_llm_channel(tok)
    assert mdl == "pinned-model"
    assert client.kwargs["base_url"] == "http://gw"
    assert client.kwargs["api_key"] == "sk-pin"
    # the pin is scoped, not resident: after reset the shell raises again
    with pytest.raises(NoActiveChannelError):
        shell._call_channel()


def test_per_field_priority_explicit_overrides_contextvar(clients):
    shell = OpenAILLM(require_channel=True)
    tok = set_request_llm_channel(("ctx-model", "http://ctx", "sk-ctx"))
    try:
        # only base_url overridden (e.g. a vision route): the rest still rides the pin
        client, mdl = shell._call_channel(base_url="http://explicit")
    finally:
        reset_request_llm_channel(tok)
    assert client.kwargs["base_url"] == "http://explicit"
    assert client.kwargs["api_key"] == "sk-ctx"
    assert mdl == "ctx-model"


def test_plain_client_keeps_embedded_config_behavior(clients):
    plain = OpenAILLM(model="self-m")
    client, mdl = plain._call_channel()
    assert client is plain.client and mdl == "self-m"


async def test_gather_children_inherit_the_pinned_channel(clients):
    shell = OpenAILLM(require_channel=True)
    tok = set_request_llm_channel(("m", "http://gw", "sk-pin"))
    try:
        async def probe():
            client, _ = shell._call_channel()
            return client.kwargs["api_key"]

        assert await asyncio.gather(probe(), probe()) == ["sk-pin", "sk-pin"]
    finally:
        reset_request_llm_channel(tok)


# -- 3: worker _run pinning -------------------------------------------------------------

from core.infrastructure import llm_routing

from apps.worker import tasks


class _Store:
    def __init__(self, owner):
        self.owner = owner
        self.running: list = []
        self.succeeded: list = []
        self.failed: list = []

    async def get(self, job_id):
        return SimpleNamespace(user_id=self.owner)

    async def mark_running(self, job_id, error=None):
        self.running.append(error)

    async def mark_succeeded(self, job_id, result):
        self.succeeded.append(result)

    async def mark_failed(self, job_id, error):
        self.failed.append(error)


def _patch_resolve(monkeypatch, result):
    async def fake(session_factory, user_id):
        return result

    monkeypatch.setattr(llm_routing, "resolve_channel_for_owner", fake)


async def test_run_pins_channel_for_owner_and_resets_in_finally(monkeypatch):
    uid = uuid.uuid4()
    _patch_resolve(monkeypatch, ("http://gw", "sk-secret", "model-x", "biz", uuid.uuid4()))
    seen = {}

    async def work():
        seen["chan"] = get_request_llm_channel()
        return {"ok": True}

    ctx = {"job_store": _Store(uid), "job_try": 1, "session_factory": None}
    result = await tasks._run(ctx, uuid.uuid4().__str__(), work())
    # pinned INSIDE the job …
    assert seen["chan"] == ("model-x", "http://gw", "sk-secret")
    # … and gone after: a reused coroutine context must not inherit the credentials
    assert result == {"ok": True}
    assert get_request_llm_channel() is None


async def test_run_reuses_context_safely_across_jobs(monkeypatch):
    """Job 2 (no channel) must not see job 1's pin, even on the same context."""
    uid = uuid.uuid4()
    ctx = {"job_store": _Store(uid), "job_try": 1, "session_factory": None}

    async def sees_channel():
        return get_request_llm_channel()

    _patch_resolve(monkeypatch, ("http://gw", "sk-secret", "m", "b", uuid.uuid4()))
    first = await tasks._run(ctx, str(uuid.uuid4()), sees_channel())
    assert first == ("m", "http://gw", "sk-secret")
    _patch_resolve(monkeypatch, ("", "", "", "", None))
    second = await tasks._run(ctx, str(uuid.uuid4()), sees_channel())
    assert second is None  # the first job's credentials did NOT leak


async def test_run_no_owner_pins_nothing(monkeypatch):
    async def boom(session_factory, user_id):
        raise AssertionError("no-owner job must not hit the gateway at all")

    monkeypatch.setattr(llm_routing, "resolve_channel_for_owner", boom)
    ctx = {"job_store": _Store(None), "job_try": 1, "session_factory": None}

    async def work():
        return get_request_llm_channel()

    assert await tasks._run(ctx, str(uuid.uuid4()), work()) is None


async def test_run_shell_call_without_channel_lands_in_job_error(monkeypatch, tmp_path):
    uid = uuid.uuid4()
    _patch_resolve(monkeypatch, ("", "", "", "", None))
    monkeypatch.setattr(tasks.settings, "worker_max_tries", 1)  # first attempt is terminal
    monkeypatch.setattr(tasks.settings, "audit_log_path", tmp_path / "audit.jsonl")
    shell = OpenAILLM(require_channel=True)
    store = _Store(uid)
    ctx = {"job_store": store, "job_try": 1, "session_factory": None}

    async def work():
        shell._call_channel()  # the exact failure mode doctrine demands
        return {}

    with pytest.raises(NoActiveChannelError):
        await tasks._run(ctx, str(uuid.uuid4()), work())
    assert store.failed and "no gateway-resolved LLM channel" in store.failed[0]


# -- 4: toolkit enqueue gate -------------------------------------------------------------

from api.routers import jobs as jobs_mod
from api.schemas import ToolkitGenerateRequest
from fastapi import HTTPException


class _Queue:
    def __init__(self):
        self.enqueued: list = []

    async def enqueue(self, type_, payload, user_id=None):
        self.enqueued.append((type_, payload, user_id))
        return uuid.uuid4()


def _user():
    return SimpleNamespace(user_id=uuid.uuid4(), role=SimpleNamespace(role_id="pro"))


class _SessionCm:
    def __init__(self, row=None):
        self.row = row

    def __call__(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, model, pk):
        return self.row


async def test_toolkit_gate_blocks_enqueue_without_channel(monkeypatch):
    async def ok_usage(session, uid, role):
        return "free"

    async def no_channel(session, **kw):
        return ("", "", "", "", None)

    monkeypatch.setattr(jobs_mod, "authorize_usage", ok_usage)
    monkeypatch.setattr(jobs_mod, "resolve_effective_channel", no_channel)
    monkeypatch.setattr(jobs_mod, "SessionLocal", _SessionCm())
    queue = _Queue()
    user = _user()
    body = ToolkitGenerateRequest(tool="slides", session_id=uuid.uuid4())

    with pytest.raises(HTTPException) as exc:
        await jobs_mod.generate_toolkit(body, queue=queue, user=user)
    assert exc.value.status_code == 503
    assert queue.enqueued == []  # the job row is NEVER created


async def test_toolkit_session_mode_payload_carries_audit_not_keys(monkeypatch):
    cred_id = uuid.uuid4()
    user = _user()
    session_row = SimpleNamespace(user_id=user.user_id)

    async def ok_usage(session, uid, role):
        return "free"

    async def channel(session, **kw):
        return ("http://gw", "sk-live-secret", "model-x", "biz", cred_id)

    monkeypatch.setattr(jobs_mod, "authorize_usage", ok_usage)
    monkeypatch.setattr(jobs_mod, "resolve_effective_channel", channel)
    monkeypatch.setattr(jobs_mod, "SessionLocal", _SessionCm(session_row))
    queue = _Queue()
    sid = uuid.uuid4()
    body = ToolkitGenerateRequest(tool="slides", session_id=sid)

    await jobs_mod.generate_toolkit(body, queue=queue, user=user)
    assert len(queue.enqueued) == 1
    _type, payload, enq_user = queue.enqueued[0]
    assert enq_user == user.user_id
    audit = payload["llm_audit"]
    assert audit["credential_id"] == str(cred_id)
    assert audit["provider_model"] == "model-x"
    assert len(audit["key_fp"]) == 12
    # plaintext-key red line: neither the key nor any payload channel field exists
    assert "api_key" not in payload
    assert "sk-live-secret" not in json.dumps(payload)
    # SSRF red line: payload never offers the worker a base_url to honor
    assert payload.get("base_url") is None


# -- 5: static whitelist guard -----------------------------------------------------------

SHELL_REQUIRED = {
    "apps/worker/settings.py",   # worker ctx["llm"] — every job rides the gateway pin
    "apps/retrieval/main.py",    # retrieval service — pinned per Retrieve request
}
# Production-adjacent constructions allowed to keep the embedded-config posture,
# each with the reason it is NOT a resident-commercial-key bypass on a core path.
ALLOW_NOT_SHELL = {
    "apps/api/agent_factory.py": "API interactive singleton: the router-level gateway "
    "503s before any call when no channel resolves, and product calls pin the ContextVar",
    "plugins/research/plugin.py": "adjudicate/vision singletons pass the pinned ContextVar "
    "channel explicitly per call",
    "scripts/smoke_test_slides.py": "local diagnostic tool",
    "scripts/eval_rag.py": "local diagnostic tool",
}


def test_every_openai_llm_construction_is_shell_or_allowlisted():
    sites: dict[str, list[bool]] = {}
    for base in ("apps", "packages", "plugins", "scripts"):
        for py in (REPO / base).rglob("*.py"):
            rel = py.relative_to(REPO).as_posix()
            text = py.read_text(encoding="utf-8")
            for m in re.finditer(r"OpenAILLM\((.*?)\)", text, flags=re.DOTALL):
                sites.setdefault(rel, []).append("require_channel=True" in m.group(1))
    for rel, flags in sites.items():
        for is_shell in flags:
            if rel in SHELL_REQUIRED:
                assert is_shell, f"{rel} must construct the client as a shell"
            else:
                assert is_shell or rel in ALLOW_NOT_SHELL, (
                    f"new resident-key OpenAILLM() at {rel}: core business clients must "
                    "pass require_channel=True; add to ALLOW_NOT_SHELL with a reason only "
                    "if this is a test/diagnostic/pricing-only entry"
                )
    # the two shell anchors still exist (a rename must not silently drop the guard)
    assert SHELL_REQUIRED <= set(sites)
