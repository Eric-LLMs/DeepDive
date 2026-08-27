"""Security regressions for the P0 fixes: guest-identity forgery, wallet gating, path confinement.

Each test targets a pure decision at the helper boundary (no live DB/Redis), matching the
style of ``test_auth_rate_limit.py``: the signed guest token is self-contained, the free-vs-
paid gate in ``authorize_usage`` only depends on counters + balance, and ``_confined_path``
is pure path confinement. The route-level 401/403/404 wiring (``Depends(require_user)`` and
session-ownership checks) lives in the routers; the invariants underneath are covered here.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from api import auth as auth_mod
from api.routers import _shared
from api.routers.jobs import _confined_path
from core.config import settings
from core.infrastructure import security as sec
from fastapi import HTTPException

_TEST_SECRET = "test-console-secret"


@pytest.fixture(autouse=True)
def _fixed_console_secret(monkeypatch):
    """Pin the HMAC secret so token tests never touch the DB-backed console secret."""

    async def fake_secret() -> str:
        return _TEST_SECRET

    monkeypatch.setattr(auth_mod, "_console_secret", fake_secret)


# ── A3: signed guest tokens ────────────────────────────────────────────────


async def test_guest_token_round_trip():
    uid = uuid4()
    token = await auth_mod.sign_guest_token(uid, datetime.now(UTC) + timedelta(days=1))
    assert token.startswith("gt_")
    assert await auth_mod.verify_guest_token(token) == uid


async def test_guest_token_expired_rejected():
    uid = uuid4()
    token = await auth_mod.sign_guest_token(uid, datetime.now(UTC) - timedelta(seconds=1))
    assert await auth_mod.verify_guest_token(token) is None


async def test_guest_token_tampered_signature_rejected():
    uid = uuid4()
    token = await auth_mod.sign_guest_token(uid, datetime.now(UTC) + timedelta(days=1))
    last = token[-1]
    tampered = token[:-1] + ("0" if last != "0" else "1")
    assert await auth_mod.verify_guest_token(tampered) is None


async def test_guest_token_wrong_prefix_rejected():
    # A `cc_` console-style token (or any other scheme) must not verify as a guest token.
    assert await auth_mod.verify_guest_token("cc_" + "a" * 80) is None
    assert await auth_mod.verify_guest_token("garbage") is None


async def test_guest_token_corrupted_payload_rejected():
    uid = uuid4()
    token = await auth_mod.sign_guest_token(uid, datetime.now(UTC) + timedelta(days=1))
    # Truncating mid-payload corrupts the base64 body (decode fails or yields garbage).
    assert await auth_mod.verify_guest_token(token[:20] + ".fakesig") is None


async def test_resolve_guest_identity_uses_embedded_uid_and_mints_nothing(monkeypatch):
    """A valid token short-circuits: the embedded uid wins, no user is created, no token minted.

    The client can never steer the identity — ``resolve_guest_identity`` takes no user_id
    argument at all; a forged ``body.user_id`` is simply not part of the anonymous contract.
    """
    uid = uuid4()

    async def fake_verify(tok):
        return uid

    def boom(*a, **k):
        raise AssertionError("must not create a user or mint a token for a valid guest token")

    monkeypatch.setattr(_shared, "verify_guest_token", fake_verify)
    monkeypatch.setattr(_shared, "ensure_user", boom)
    monkeypatch.setattr(_shared, "sign_guest_token", boom)

    resolved_uid, new_token = await _shared.resolve_guest_identity(None, "some-valid-token")
    assert resolved_uid == uid
    assert new_token is None


async def test_resolve_guest_identity_mints_fresh_guest_when_token_invalid(monkeypatch):
    created = uuid4()

    async def fake_verify(tok):
        return None

    async def fake_ensure_user(session_factory, user_id=None):
        return created

    async def fake_sign(uid, expires_at):
        return "gt_fresh"

    monkeypatch.setattr(_shared, "verify_guest_token", fake_verify)
    monkeypatch.setattr(_shared, "ensure_user", fake_ensure_user)
    monkeypatch.setattr(_shared, "sign_guest_token", fake_sign)

    uid, token = await _shared.resolve_guest_identity(None, "bad-or-expired-token")
    assert uid == created
    assert token == "gt_fresh"


async def test_resolve_guest_identity_mints_fresh_guest_when_no_token(monkeypatch):
    created = uuid4()

    async def fake_verify(tok):
        return None

    async def fake_ensure_user(session_factory, user_id=None):
        return created

    async def fake_sign(uid, expires_at):
        return "gt_fresh"

    monkeypatch.setattr(_shared, "verify_guest_token", fake_verify)
    monkeypatch.setattr(_shared, "ensure_user", fake_ensure_user)
    monkeypatch.setattr(_shared, "sign_guest_token", fake_sign)

    uid, token = await _shared.resolve_guest_identity(None, None)
    assert uid == created
    assert token == "gt_fresh"


# ── A5: free-quota-first wallet gate ───────────────────────────────────────


def _role(**kw) -> SimpleNamespace:
    return SimpleNamespace(
        daily_request_limit=kw.get("daily_request_limit", 10),
        monthly_request_limit=kw.get("monthly_request_limit", -1),
        daily_token_limit=kw.get("daily_token_limit", -1),
    )


class _FakeSession:
    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1


async def test_authorize_usage_free_increments_and_commits(monkeypatch):
    session = _FakeSession()
    increments = []

    async def fake_get_usage(s, uid, pt, ps):
        return 0, 0

    async def fake_increment_usage(s, uid, pt, ps, requests=1, tokens=0):
        increments.append((pt, ps, requests, tokens))
        return requests, tokens

    monkeypatch.setattr(sec, "get_usage", fake_get_usage)
    monkeypatch.setattr(sec, "increment_usage", fake_increment_usage)

    tier = await sec.authorize_usage(session, uuid4(), _role())
    assert tier == "free"
    assert [c[0] for c in increments] == ["day", "month"]  # both periods counted
    assert session.commits == 1


async def test_authorize_usage_overflow_low_balance_402(monkeypatch):
    session = _FakeSession()
    increments = []

    async def fake_get_usage(s, uid, pt, ps):
        return 50, 0  # day counter already past the role limit

    async def fake_increment_usage(*a, **k):
        increments.append(a)
        return 0, 0

    async def fake_get_balance(s, uid):
        return 0.0  # at the gate floor

    monkeypatch.setattr(sec, "get_usage", fake_get_usage)
    monkeypatch.setattr(sec, "increment_usage", fake_increment_usage)
    monkeypatch.setattr(sec, "get_balance", fake_get_balance)

    with pytest.raises(HTTPException) as exc:
        await sec.authorize_usage(session, uuid4(), _role(daily_request_limit=10))
    assert exc.value.status_code == 402
    assert increments == []  # free counters must not move on a rejected overflow
    assert session.commits == 0


async def test_authorize_usage_overflow_with_balance_returns_paid(monkeypatch):
    session = _FakeSession()
    increments = []

    async def fake_get_usage(s, uid, pt, ps):
        return 50, 0

    async def fake_increment_usage(*a, **k):
        increments.append(a)
        return 0, 0

    async def fake_get_balance(s, uid):
        return 25.0

    monkeypatch.setattr(sec, "get_usage", fake_get_usage)
    monkeypatch.setattr(sec, "increment_usage", fake_increment_usage)
    monkeypatch.setattr(sec, "get_balance", fake_get_balance)

    assert await sec.authorize_usage(session, uuid4(), _role(daily_request_limit=10)) == "paid"
    assert increments == []  # overflow does not consume free quota
    assert session.commits == 0


# ── A2: /media/generate path confinement ───────────────────────────────────


async def test_confined_path_accepts_inside_workspace(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    video = tmp_path / "video.mp4"
    video.touch()
    assert _confined_path(str(video), field="video_path") == str(video.resolve())


async def test_confined_path_rejects_outside_workspace(monkeypatch, tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("s")
    monkeypatch.setattr(settings, "workspace_dir", str(root))

    with pytest.raises(HTTPException) as exc:
        _confined_path(str(outside / "secret.txt"), field="video_path")
    assert exc.value.status_code == 400


async def test_confined_path_rejects_parent_escape(monkeypatch, tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    monkeypatch.setattr(settings, "workspace_dir", str(root))

    with pytest.raises(HTTPException) as exc:
        _confined_path(str(root / "sub" / ".." / ".." / "etc" / "passwd"), field="video_path")
    assert exc.value.status_code == 400
