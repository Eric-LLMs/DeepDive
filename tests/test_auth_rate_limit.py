"""Auth rate limiting (fixed-window Redis counter): check_rate_limit + _auth_rate_limit.

These guard /auth/login·/auth/register·/auth/forgot-password·/auth/reset-password·
/auth/resend-verification against brute force and mail-bombing. The window starts at the
first hit (INCR+EXPIRE-on-first), so a quiet burst then idle resets naturally.
"""
from __future__ import annotations

import pytest
from api.routers._shared import _auth_rate_limit, check_rate_limit
from core.config import settings
from fastapi import HTTPException


class FakeRedis:
    """Minimal in-memory Redis fake: incr/expire, with optional failure injection."""

    def __init__(self, fail_on: str | None = None) -> None:
        self.store: dict[str, int] = {}
        self.expirations: dict[str, int] = {}
        self.fail_on = fail_on

    async def incr(self, key):
        if self.fail_on == "incr":
            raise ConnectionError("redis unavailable")
        self.store[key] = self.store.get(key, 0) + 1
        return self.store[key]

    async def expire(self, key, window):
        if self.fail_on == "expire":
            raise ConnectionError("redis unavailable")
        self.expirations[key] = window


class FakeRequest:
    def __init__(self, host: str = "203.0.113.7", redis=None) -> None:
        self.client = type("Client", (), {"host": host})()
        self.app = type("App", (), {"state": type("State", (), {"redis": redis})()})()


# ── check_rate_limit ────────────────────────────────────────────────────────


async def test_limit_zero_disables_check():
    assert await check_rate_limit(FakeRedis(), "k", 0, 60) is True
    assert await check_rate_limit(None, "k", 0, 60) is True


async def test_first_call_sets_expiry():
    redis = FakeRedis()
    assert await check_rate_limit(redis, "k", 3, 60) is True
    assert redis.store["k"] == 1
    assert redis.expirations["k"] == 60  # window anchored at the first hit


async def test_within_window_allowed_up_to_limit():
    redis = FakeRedis()
    for _ in range(3):
        assert await check_rate_limit(redis, "k", 3, 60) is True


async def test_exceeding_limit_blocked():
    redis = FakeRedis()
    for _ in range(3):
        await check_rate_limit(redis, "k", 3, 60)
    assert await check_rate_limit(redis, "k", 3, 60) is False


async def test_window_per_key_independent():
    redis = FakeRedis()
    await check_rate_limit(redis, "a", 1, 60)
    assert await check_rate_limit(redis, "a", 1, 60) is False
    assert await check_rate_limit(redis, "b", 1, 60) is True  # fresh key unaffected


async def test_redis_failure_fails_open():
    # A Redis outage must never turn into a hard 429 on the login path.
    redis = FakeRedis(fail_on="incr")
    assert await check_rate_limit(redis, "k", 3, 60) is True


# ── _auth_rate_limit ────────────────────────────────────────────────────────


async def test_unknown_kind_is_noop():
    await _auth_rate_limit(FakeRequest(redis=FakeRedis()), FakeRedis(), "bogus")


async def test_none_redis_is_noop():
    await _auth_rate_limit(FakeRequest(redis=None), None, "login")


async def test_disabled_limit_is_noop(monkeypatch):
    monkeypatch.setattr(settings, "auth_login_rpm", 0)
    await _auth_rate_limit(FakeRequest(redis=FakeRedis()), FakeRedis(), "login")


async def test_raises_429_when_limit_exhausted(monkeypatch):
    monkeypatch.setattr(settings, "auth_login_rpm", 2)
    redis = FakeRedis()
    for _ in range(2):
        await _auth_rate_limit(FakeRequest(redis=redis), redis, "login")
    with pytest.raises(HTTPException) as exc:
        await _auth_rate_limit(FakeRequest(redis=redis), redis, "login")
    assert exc.value.status_code == 429


async def test_ip_isolates_counters(monkeypatch):
    monkeypatch.setattr(settings, "auth_login_rpm", 1)
    redis = FakeRedis()
    await _auth_rate_limit(FakeRequest(host="198.51.100.1", redis=redis), redis, "login")
    # A different client IP has its own window and is not blocked.
    await _auth_rate_limit(FakeRequest(host="198.51.100.2", redis=redis), redis, "login")
