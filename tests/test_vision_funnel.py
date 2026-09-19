"""Vision routing inside the unified permission funnel (vision_caption.py).

Doctrine: image models go through the same role-authorization gates as chat models —
no unbound/global key is ever reachable. Selection prefers ``vision``-named catalog
entries, then gambles the remaining authorized models in order (a model that cannot see
images fails at call time and the chain moves on); total failure raises
``VisionUnsupported``, an empty/invalid authorization raises ``VisionNotAuthorized``.
The DB layer is exercised through pure seams (``rank_vision_entries``) and a stubbed
resolver for ``describe_image`` — same no-live-DB style as ``test_security_regression``.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from core.infrastructure import vision_caption as vc


def _cred(name: str) -> SimpleNamespace:
    return SimpleNamespace(base_url=f"https://{name}/v1", api_key=f"key-{name}")


def _model(name: str, provider: str = "") -> SimpleNamespace:
    return SimpleNamespace(name=name, provider_model_name=provider)


# ── rank_vision_entries: pure ordering over already-authorized pairs ────────


def test_configured_model_must_be_authorized_and_matches_exactly():
    pairs = [(_cred("c1"), _model("DeepDive_Chat", "deepseek-v4")),
             (_cred("c2"), _model("DeepDive_IMG", "vision-x"))]
    hits = vc.rank_vision_entries(pairs, configured="deepdive_img", role_id="admin")
    assert hits == [(pairs[1][0], pairs[1][1])]  # case-insensitive, name or provider id


def test_configured_model_not_in_authorized_set_is_refused():
    pairs = [(_cred("c1"), _model("Only_Chat"))]
    with pytest.raises(vc.VisionNotAuthorized) as exc:
        vc.rank_vision_entries(pairs, configured="DeepDive_IMG", role_id="guest")
    assert "not served by any channel" in str(exc.value)


def test_vision_named_entries_float_to_front():
    a = (_cred("c1"), _model("Alpha"))
    v = (_cred("c2"), _model("VisionPro"))
    p = (_cred("c3"), _model("Plain", "gemini-vision-lite"))
    ranked = vc.rank_vision_entries([a, v, p], configured="", role_id="admin")
    assert ranked[:2] == [v, p]  # both vision-marked (display name or provider id) first
    assert ranked[2] == a  # the rest kept as a capability gamble


def test_no_vision_named_still_gambles_all_authorized():
    a = (_cred("c1"), _model("Alpha"))
    b = (_cred("c2"), _model("Beta"))
    assert vc.rank_vision_entries([a, b], configured="", role_id="admin") == [a, b]


def test_empty_authorized_set_is_not_authorized():
    with pytest.raises(vc.VisionNotAuthorized):
        vc.rank_vision_entries([], configured="", role_id="ghost")


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


async def test_describe_image_tries_vision_first_then_gambles(monkeypatch):
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


# ── identity fail-fast: no anonymous resolution, no unbound-key fallback ────


async def test_resolve_without_identity_is_refused():
    with pytest.raises(vc.VisionNotAuthorized) as exc:
        await vc.resolve_vision_channels(None)
    assert "no user identity" in str(exc.value)
