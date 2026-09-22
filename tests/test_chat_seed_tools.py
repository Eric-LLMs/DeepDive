"""Phase 5A seed tools: the allowlisted capabilities registered as REAL agent tools.

The fast-path invariant — "every DIRECT_TOOL capability must also be LLM-callable" —
means create_folder / add_term are ordinary auto-discovered ``*_tool.py`` registrations
over the same services the REST endpoints use. These tests drive the registered tool
bodies directly (fake runtime + monkeypatched service classes) and pin:

* the tool definitions exist under the exact allowlisted names;
* provable non-execution failures surface on the ``"preflight: "`` channel the chat
  adapter maps to ActionPreflightFailure (⇒ escalate);
* add_term domain resolution (constraint 3): strip+lower EXACT match; 0 → preflight,
  >1 → explicit ambiguity preflight — NEVER ``matches[0]`` (assert zero add_term calls
  against each colliding domain);
* the request user is the only authz input: the services receive the caller's uid.
"""
from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import api.tools.add_term_tool as add_term_mod
import api.tools.create_folder_tool as create_folder_mod
import pytest
from core.application.drive_service import DriveError
from core.infrastructure.request_context import set_request_user

USER = uuid4()


class _RT:
    def __init__(self):
        self.defs = {}

    def register(self, d):
        self.defs[d.name] = d


class _Ctx:
    def __init__(self, session_factory):
        self._sf = session_factory

    def resolve(self, name):
        assert name == "session_factory"
        return self._sf


class _SessionCtx:
    async def __aenter__(self):
        return "session"

    async def __aexit__(self, *a):
        return False


def _factory():
    return _SessionCtx()


def _register(module):
    rt = _RT()
    module.register(rt, _Ctx(_factory), llm=None)
    return rt.defs


@pytest.fixture(autouse=True)
def _auth():
    set_request_user(USER)
    yield
    set_request_user(None)


# ── create_folder ──────────────────────────────────────────────────────────────────

def test_create_folder_registered():
    defs = _register(create_folder_mod)
    assert "create_folder" in defs


async def test_create_folder_success_scoped_to_caller(monkeypatch):
    calls = []

    class _Drive:
        def __init__(self, session_factory):
            pass

        async def create_folder(self, user_id, workspace_id, parent_path, name):
            calls.append((user_id, workspace_id, parent_path, name))
            return {"path": name, "id": "f1"}

    monkeypatch.setattr(create_folder_mod, "DriveService", _Drive)
    defs = _register(create_folder_mod)
    out = await defs["create_folder"].execute({"name": "archive"}, None)
    assert calls == [(USER, None, None, "archive")]
    assert "archive" in out


async def test_create_folder_drive_error_becomes_preflight(monkeypatch):
    class _Drive:
        def __init__(self, session_factory):
            pass

        async def create_folder(self, *a):
            raise DriveError("no access to folder", 403)

    monkeypatch.setattr(create_folder_mod, "DriveService", _Drive)
    defs = _register(create_folder_mod)
    with pytest.raises(ValueError, match="^preflight: "):
        await defs["create_folder"].execute({"name": "x"}, None)


async def test_create_folder_requires_request_user():
    set_request_user(None)
    defs = _register(create_folder_mod)
    with pytest.raises(ValueError, match="preflight: .*authenticated"):
        await defs["create_folder"].execute({"name": "x"}, None)


# ── add_term (constraint 3: unique domain resolution) ──────────────────────────────

class _FakeVocab:
    last = None

    def __init__(self, domains, *_a, **_k):
        self._domains = domains
        self.added = []
        type(self).last = self

    async def list_domains(self, user_id):
        self.listed_user = user_id
        return self._domains

    async def add_term(self, domain_id, word, definition="", user_id=None):
        self.added.append((domain_id, word, definition, user_id))
        return SimpleNamespace(word=word)


def _domains(*names):
    return [SimpleNamespace(id=f"d{i}", name=n) for i, n in enumerate(names)]


def _vocab_with(domains, monkeypatch):
    monkeypatch.setattr(add_term_mod, "VocabularyService",
                        lambda *a, **k: _FakeVocab(domains))


def test_add_term_registered():
    defs = _register(add_term_mod)
    assert "add_term" in defs


async def test_add_term_exact_match_ignores_case_and_spaces(monkeypatch):
    doms = _domains("English 101", "physics")
    _vocab_with(doms, monkeypatch)
    defs = _register(add_term_mod)
    out = await defs["add_term"].execute({"term": " entropy ", "domain": "  PHYSICS "}, None)
    assert _FakeVocab.last.added == [("d1", "entropy", "", USER)]
    assert "physics" in out.lower()


async def test_add_term_domain_not_found_is_preflight_no_write(monkeypatch):
    _vocab_with(_domains("english"), monkeypatch)
    defs = _register(add_term_mod)
    with pytest.raises(ValueError, match="preflight: .*not found"):
        await defs["add_term"].execute({"term": "x", "domain": "spanish"}, None)
    assert _FakeVocab.last.added == []


async def test_add_term_ambiguous_never_picks_first(monkeypatch):
    doms = _domains("english", "english")  # two visible domains, same name
    _vocab_with(doms, monkeypatch)
    defs = _register(add_term_mod)
    with pytest.raises(ValueError, match="preflight: .*2 vocabulary domains"):
        await defs["add_term"].execute({"term": "x", "domain": "English"}, None)
    # NEVER matches[0]: no add_term call against EITHER colliding domain.
    assert _FakeVocab.last.added == []


async def test_add_term_requires_both_slots(monkeypatch):
    _vocab_with([], monkeypatch)
    defs = _register(add_term_mod)
    with pytest.raises(ValueError, match="preflight:"):
        await defs["add_term"].execute({"term": "x", "domain": "  "}, None)
