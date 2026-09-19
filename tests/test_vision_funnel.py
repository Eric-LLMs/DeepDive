"""Vision routing tests (core.infrastructure.vision_caption).

Doctrine under test: the model source is ONLY the caller's role-authorized database
list — vision-marked names (vision / multimodal / 4o / vl) tried first, the rest of the
authorized list as a capability gamble, all-fail → VisionUnsupported ("image processing
is not supported"). A logged-in role without models is downgraded onto the anonymous
tier with the user's guest/free allowance consumed through the existing authorize_usage
path (over the allowance → explicit refusal); true guests meter at the chat entry, not
again inside vision. No global/config key is consulted anywhere. The DB layer is
exercised through pure seams (``rank_vision_entries``) and stubbed
``_authorized_pairs`` / resolver.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from core.infrastructure import vision_caption as vc
from fastapi import HTTPException


def _cred(name: str) -> SimpleNamespace:
    return SimpleNamespace(base_url=f"https://{name}/v1", api_key=f"key-{name}")


def _model(name: str, provider: str = "") -> SimpleNamespace:
    return SimpleNamespace(name=name, provider_model_name=provider)


class _Session:
    """Async-context-manager stub; every DB accessor is monkeypatched in these tests."""

    def __init__(self, user=None):
        self._user = user

    async def get(self, _model_cls, _uid):
        return self._user

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


# ── rank_vision_entries: pure ordering over already-authorized pairs ────────


def test_vision_markers_float_to_front():
    a = (_cred("c1"), _model("Alpha"))
    v = (_cred("c2"), _model("VisionPro"))
    m = (_cred("c3"), _model("GPT", "gpt-4o-mini"))
    q = (_cred("c4"), _model("Qwen-VL", "qwen-vl-max"))
    d = (_cred("c5"), _model("豆包", "doubao-多模态"))
    ranked = vc.rank_vision_entries([a, v, m, q, d])
    assert ranked[4] == a  # the unmarked plain model sank to the gamble tail
    assert {p[1].name for p in ranked[:4]} == {"VisionPro", "GPT", "Qwen-VL", "豆包"}
    assert ranked[:4] == [v, m, q, d]  # relative priority order among marked ones kept


def test_no_marked_model_gambles_whole_list_in_order():
    a = (_cred("c1"), _model("Alpha"))
    b = (_cred("c2"), _model("Beta"))
    assert vc.rank_vision_entries([a, b]) == [a, b]


# ── resolve_vision_channels: who may be served, from which list ─────────────


async def test_guest_resolves_through_the_anonymous_role(monkeypatch):
    seen: list[str] = []
    pairs = [(_cred("c1"), _model("Chat")), (_cred("c2"), _model("AnyVision"))]

    async def fake(session_factory, *, role_id, user_id):
        seen.append(role_id)
        return pairs

    monkeypatch.setattr(vc, "_authorized_pairs", fake)
    chain = await vc.resolve_vision_channels(None)
    assert seen == [vc.ANONYMOUS_ROLE]
    assert chain == [("https://c2/v1", "key-c2", "AnyVision"), ("https://c1/v1", "key-c1", "Chat")]


async def test_modelless_role_downgrades_and_consumes_guest_allowance(monkeypatch):
    seen_roles: list[str] = []
    metered: list[tuple] = []
    anon_pairs = [(_cred("c1"), _model("GuestChat")), (_cred("c2"), _model("GuestVision"))]

    async def fake_pairs(session_factory, *, role_id, user_id):
        seen_roles.append(role_id)
        return anon_pairs if role_id == vc.ANONYMOUS_ROLE else []

    async def fake_get_role(session, role_id):
        return SimpleNamespace(role_id=role_id)

    async def fake_authorize(session, user_id, role, requests=1, tokens=0):
        metered.append((user_id, role.role_id))
        return "free"

    monkeypatch.setattr(vc, "_authorized_pairs", fake_pairs)
    monkeypatch.setattr(vc, "get_role", fake_get_role)
    monkeypatch.setattr(vc, "authorize_usage", fake_authorize)
    chain = await vc.resolve_vision_channels(lambda: _Session(), role_id="vip", user_id="u-9")
    assert seen_roles == ["vip", vc.ANONYMOUS_ROLE]
    assert metered == [("u-9", vc.ANONYMOUS_ROLE)]  # guest allowance, same counter path
    assert [m for _, _, m in chain] == ["GuestVision", "GuestChat"]


async def test_downgrade_over_guest_allowance_is_refused_explicitly(monkeypatch):
    async def fake_pairs(session_factory, *, role_id, user_id):
        return []

    async def fake_get_role(session, role_id):
        return SimpleNamespace(role_id=role_id)

    async def fake_authorize(session, user_id, role, requests=1, tokens=0):
        raise HTTPException(status_code=402, detail="quota")

    monkeypatch.setattr(vc, "_authorized_pairs", fake_pairs)
    monkeypatch.setattr(vc, "get_role", fake_get_role)
    monkeypatch.setattr(vc, "authorize_usage", fake_authorize)
    with pytest.raises(vc.VisionNotAuthorized) as exc:
        await vc.resolve_vision_channels(lambda: _Session(), role_id="vip", user_id="u-9")
    assert "free trial allowance exhausted" in str(exc.value)


async def test_true_guest_is_not_metered_inside_vision(monkeypatch):
    async def no_meter(*a, **k):  # guests were already metered at the chat entry
        raise AssertionError("authorize_usage must not run for a true guest")

    async def fake_pairs(session_factory, *, role_id, user_id):
        return [(_cred("c1"), _model("AnyVision"))]

    monkeypatch.setattr(vc, "_authorized_pairs", fake_pairs)
    monkeypatch.setattr(vc, "authorize_usage", no_meter)
    chain = await vc.resolve_vision_channels(None)
    assert chain == [("https://c1/v1", "key-c1", "AnyVision")]


async def test_user_identity_resolves_its_own_role(monkeypatch):
    seen: list[tuple] = []
    pairs = [(_cred("c1"), _model("XVision"))]

    async def fake(session_factory, *, role_id, user_id):
        seen.append((role_id, user_id))
        return pairs

    monkeypatch.setattr(vc, "_authorized_pairs", fake)
    user = SimpleNamespace(is_active=True, role_id="regular")
    chain = await vc.resolve_vision_channels(lambda: _Session(user=user), user_id="u-2")
    assert seen == [("regular", "u-2")]
    assert chain == [("https://c1/v1", "key-c1", "XVision")]


async def test_chain_dedupes_identical_routes(monkeypatch):
    same_cred = _cred("c1")
    pairs = [(same_cred, _model("A", "dup")), (same_cred, _model("B", "dup"))]

    async def fake(session_factory, *, role_id, user_id):
        return pairs

    monkeypatch.setattr(vc, "_authorized_pairs", fake)
    chain = await vc.resolve_vision_channels(None, role_id="regular")
    assert chain == [("https://c1/v1", "key-c1", "dup")]  # provider id wins, tried once


# ── describe_image: walk the chain, first real answer wins ──────────────────


class _LLM:
    def __init__(self, results):
        self._results = list(results)  # per model: exception instance or content str
        self.seen: list[tuple[str, str]] = []

    async def chat(self, messages, *, model, base_url, api_key):
        self.seen.append((model, base_url))
        r = self._results.pop(0)
        if isinstance(r, Exception):
            raise r
        return {"content": r}


async def _stub_chain(monkeypatch, chain):
    async def fake(session_factory, *, user_id=None, role_id=None):
        return chain

    monkeypatch.setattr(vc, "resolve_vision_channels", fake)


async def test_describe_image_tries_marked_first_then_gambles(monkeypatch):
    chain = [("https://v/v1", "kv", "vision-x"), ("https://t/v1", "kt", "text-y")]
    await _stub_chain(monkeypatch, chain)
    llm = _LLM([RuntimeError("402 Insufficient Balance"), "a red square"])
    out = await vc.describe_image(b"\x89PNG\r\n\x1a\n", "image/png", llm=llm,
                                  session_factory=None, user_id="u")
    assert out == "a red square"
    assert llm.seen == [("vision-x", "https://v/v1"), ("text-y", "https://t/v1")]


async def test_describe_image_empty_answer_advances_chain(monkeypatch):
    await _stub_chain(monkeypatch, [("b1", "k1", "m1"), ("b2", "k2", "m2")])
    llm = _LLM(["   ", "real caption"])
    assert await vc.describe_image(b"x", "", llm=llm, session_factory=None, role_id="r") \
        == "real caption"
    assert len(llm.seen) == 2


async def test_describe_image_all_fail_raises_unsupported(monkeypatch):
    await _stub_chain(monkeypatch, [("b1", "k1", "m1"), ("b2", "k2", "m2")])
    llm = _LLM([RuntimeError("boom1"), RuntimeError("boom2")])
    with pytest.raises(vc.VisionUnsupported) as exc:
        await vc.describe_image(b"x", "", llm=llm, session_factory=None, user_id="u")
    assert "boom1" in str(exc.value) and "boom2" in str(exc.value)
