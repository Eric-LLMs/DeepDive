"""The dispatch gateway lives in core and is ONE funnel for every entry adapter.

Locks the v2 doctrine in place:
* the ladder module imports no FastAPI (core must stay consumable by worker/retrieval);
* ``apps.api.routers._shared`` re-exports the SAME function objects (no drifted copies);
* ``resolve_effective_channel``'s AND-funnel fails to the credential-less tuple on every
  exhausted branch — and adapters add no logic of their own (the ban gate now also
  covers the headless path because ``resolve_channel_for_owner`` funnels through it).
"""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from core.infrastructure import llm_routing as r
from core.infrastructure.db import LLMCredentialModel, UserModel

# -- structural guarantees -------------------------------------------------------------

def test_funnel_module_never_imports_fastapi():
    tree = ast.parse(Path(r.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    offenders = [m for m in imported if m == "fastapi" or m.startswith("fastapi.")]
    assert not offenders, f"dispatch gateway must not depend on the web layer: {offenders}"


def test_shared_router_layer_reexports_the_same_callables():
    from apps.api.routers import _shared

    assert _shared.resolve_effective_channel is r.resolve_effective_channel
    assert _shared._resolve_chat_route is r.resolve_chat_route
    assert _shared.resolve_channel_for_owner is r.resolve_channel_for_owner
    assert _shared._pick_credential is r.pick_credential
    assert _shared._user_banned_from is r.user_banned_from
    assert _shared._channel_route is r.channel_route
    assert _shared._fallback_model is r.fallback_model
    assert _shared._provider_model_name is r.provider_model_name


# -- test doubles -----------------------------------------------------------------------

class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return self

    def all(self):
        return list(self._value or [])


class FakeSession:
    """Feeds ``execute`` results from a queue; ``get`` answers from a model→object map."""

    def __init__(self, responses=(), objects=None):
        self.responses = list(responses)
        self.objects = objects or {}

    async def execute(self, stmt):
        return _Result(self.responses.pop(0) if self.responses else None)

    async def get(self, model, pk):
        return self.objects.get(model)


class _Factory:
    """Async-context-manager session factory (mirrors SessionLocal usage)."""

    def __init__(self, session):
        self.session = session

    def __call__(self):
        return self

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *exc):
        return False


# -- resolve_effective_channel funnel ----------------------------------------------------

async def test_no_role_binding_returns_credentialless_tuple(monkeypatch):
    async def none_pick(session, role_id, user_id=None):
        return None

    monkeypatch.setattr(r, "pick_credential", none_pick)
    out = await r.resolve_effective_channel(FakeSession(), user_id=uuid4(), role_id="regular")
    assert out == ("", "", "", "", None)


async def test_active_pinned_channel_is_routed_directly(monkeypatch):
    cred_id = uuid4()
    cred = SimpleNamespace(id=cred_id, is_active=True, base_url="http://gw", api_key="sk-x")

    async def not_banned(session, user_id, credential_id):
        return False

    async def route(session, credential, role_id):
        return (credential.base_url, credential.api_key, "prov", "biz", credential.id)

    monkeypatch.setattr(r, "user_banned_from", not_banned)
    monkeypatch.setattr(r, "channel_route", route)
    token = SimpleNamespace(user_id=uuid4(), credential_id=cred_id)
    out = await r.resolve_effective_channel(
        FakeSession(objects={LLMCredentialModel: cred}), role_id="pro", token=token
    )
    assert out == ("http://gw", "sk-x", "prov", "biz", cred_id)


async def test_disabled_pin_fails_over_to_another_active_channel(monkeypatch):
    pinned, alt = uuid4(), uuid4()
    alt_cred = SimpleNamespace(id=alt, is_active=True, base_url="http://alt", api_key="sk-alt")

    async def not_banned(session, user_id, credential_id):
        return False

    async def pick(session, role_id, user_id=None):
        return alt

    async def route(session, credential, role_id):
        return (credential.base_url, credential.api_key, "p", "b", credential.id)

    monkeypatch.setattr(r, "user_banned_from", not_banned)
    monkeypatch.setattr(r, "pick_credential", pick)
    monkeypatch.setattr(r, "channel_route", route)
    # session.get: pinned id → inactive row; alt id → active row.
    rows = {pinned: SimpleNamespace(id=pinned, is_active=False), alt: alt_cred}

    class GetSession(FakeSession):
        async def get(self, model, pk):
            return rows.get(pk)

    token = SimpleNamespace(user_id=uuid4(), credential_id=pinned)
    out = await r.resolve_effective_channel(GetSession(), role_id="pro", token=token)
    assert out == ("http://alt", "sk-alt", "p", "b", alt)


async def test_no_channel_at_all_yields_credentialless_never_a_key(monkeypatch):
    """Pinned credential gone AND no role candidates → credential-less tuple.

    The empty tuple (not an exception, not a global key) is the gateway's contract:
    the caller turns it into a 503 or a job fail-fast.
    """
    pinned = uuid4()

    async def not_banned(session, user_id, credential_id):
        return False

    async def none_pick(session, role_id, user_id=None):
        return None

    async def no_model(session, role_id=None):
        return ""

    async def passthrough(session, display):
        return display

    monkeypatch.setattr(r, "user_banned_from", not_banned)
    monkeypatch.setattr(r, "pick_credential", none_pick)
    monkeypatch.setattr(r, "fallback_model", no_model)
    monkeypatch.setattr(r, "provider_model_name", passthrough)
    token = SimpleNamespace(user_id=uuid4(), credential_id=pinned)
    out = await r.resolve_effective_channel(FakeSession(), role_id="pro", token=token)
    assert out[:2] == ("", "") and out[4] is None


async def test_channel_route_model_ladder_role_default_then_route_then_catalog(monkeypatch):
    cred = SimpleNamespace(id=uuid4(), base_url="http://gw", api_key="sk-x")
    seen: list[str] = []

    async def role_with_default(session, role_id):
        return SimpleNamespace(default_model="Pro Model")

    monkeypatch.setattr(r, "get_role", role_with_default)

    async def mapped(session, display):
        seen.append(display)
        return "provider/pro-v3"

    monkeypatch.setattr(r, "provider_model_name", mapped)
    out = await r.channel_route(FakeSession(), cred, "vip")
    # gate 3a: the role's default_model wins without touching route/catalog queries
    assert out == ("http://gw", "sk-x", "provider/pro-v3", "Pro Model", cred.id)
    assert seen == ["Pro Model"]


async def test_channel_route_without_role_or_route_falls_back_to_catalog(monkeypatch):
    cred = SimpleNamespace(id=uuid4(), base_url="http://gw", api_key="sk-x")
    catalog_row = SimpleNamespace(name="m1", provider_model_name="deep/v3")

    async def no_role(session, role_id):
        return None

    async def passthrough_provider(session, display):
        # provider lookup miss → id round-trips (legacy config compatibility)
        return display

    monkeypatch.setattr(r, "get_role", no_role)
    monkeypatch.setattr(r, "provider_model_name", passthrough_provider)
    # execute queue: credential route (None → skip) then fallback_model's first-active row
    session = FakeSession(responses=[None, catalog_row])
    out = await r.channel_route(session, cred, "regular")
    assert out == ("http://gw", "sk-x", "m1", "m1", cred.id)


# -- adapters add no logic of their own ---------------------------------------------------

async def test_chat_route_adapter_delegates_to_funnel(monkeypatch):
    captured = {}
    token = SimpleNamespace(user_id=uuid4(), credential_id=None)

    async def funnel(session, *, user_id=None, role_id=None, token=None):
        captured.update(user_id=user_id, role_id=role_id, token=token)
        return ("http://gw", "sk-x", "m", "b", None)

    monkeypatch.setattr(r, "resolve_effective_channel", funnel)
    out = await r.resolve_chat_route(FakeSession(), token, "pro")
    assert out[0] == "http://gw"
    assert captured["token"] is token and captured["role_id"] == "pro"


async def test_owner_adapter_blocks_inactive_user_and_role(monkeypatch):
    uid = uuid4()
    calls: list = []

    async def funnel(session, *, user_id=None, role_id=None, token=None):
        calls.append((user_id, role_id))
        return ("http://gw", "sk-x", "m", "b", None)

    monkeypatch.setattr(r, "resolve_effective_channel", funnel)

    async def get_role_inactive(session, role_id):
        return SimpleNamespace(is_active=False)

    monkeypatch.setattr(r, "get_role", get_role_inactive)
    user = SimpleNamespace(is_active=True, role_id="pro")
    out = await r.resolve_channel_for_owner(
        _Factory(FakeSession(objects={UserModel: user})), uid
    )
    assert out == ("", "", "", "", None) and not calls  # inactive role → refused before the funnel

    async def get_role_active(session, role_id):
        return SimpleNamespace(is_active=True)

    monkeypatch.setattr(r, "get_role", get_role_active)
    out = await r.resolve_channel_for_owner(
        _Factory(FakeSession(objects={UserModel: user})), uid
    )
    assert out[0] == "http://gw"
    # the headless path passes user_id INTO the funnel → the user-ban gate applies here too
    assert calls == [(uid, "pro")]


async def test_owner_adapter_blocks_missing_or_deactivated_account():
    out = await r.resolve_channel_for_owner(_Factory(FakeSession(objects={})), uuid4())
    assert out == ("", "", "", "", None)
    banned_user = SimpleNamespace(is_active=False, role_id="pro")
    out = await r.resolve_channel_for_owner(
        _Factory(FakeSession(objects={UserModel: banned_user})), uuid4()
    )
    assert out == ("", "", "", "", None)
