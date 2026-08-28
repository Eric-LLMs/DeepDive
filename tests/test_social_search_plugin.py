"""Mock-network unit tests for ``plugins/social_search/plugin.py``.

The ``search_social`` tool's platform adapters all follow the same shape
(``httpx.AsyncClient`` GET → normalize), so the whole network layer is stubbed:
no real sockets, no real Reddit/X. These tests pin down the plugin *contract* —
the unified item schema, platform error mapping, and the ``auto`` merge policy —
without depending on external services.
"""
import os
import unittest.mock as mock

import httpx
import pytest

from agent.tools.tool_permissions import ToolPermission
from core.config import export_secret_env, settings
from plugins.social_search import plugin as mod


# ── network doubles ──────────────────────────────────────────────────────────
_REDDIT_PAYLOAD = {
    "data": {
        "children": [
            {
                "data": {
                    "title": "Great thread",
                    "selftext": "x" * 2000,  # longer than the snippet cap
                    "author": "alice",
                    "score": 42,
                    "num_comments": 7,
                    "url": "/r/test/comments/abc/great_thread/",
                    "created_utc": 1234567890,
                }
            }
        ]
    }
}

_X_PAYLOAD = {
    "data": [
        {
            "id": "1",
            "text": "hello",
            "author_id": "bob",
            "public_metrics": {"like_count": 3, "retweet_count": 1, "reply_count": 0},
            "created_at": "2026-01-01T00:00:00Z",
        }
    ]
}


def _http_error(status_code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "https://www.reddit.com/search.json")
    return httpx.HTTPStatusError("boom", request=req, response=httpx.Response(status_code, request=req))


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self._status_code = status_code

    @property
    def status_code(self):
        return self._status_code

    def raise_for_status(self):
        if self._status_code >= 400:
            raise _http_error(self._status_code)

    def json(self):
        return self._payload


class _FakeAsyncClient:
    """Async-context-manager ``httpx.AsyncClient`` double that records GETs/POSTs."""

    def __init__(self, payload, status_code=200):
        self._payload = payload
        self._status_code = status_code
        self.calls: list[tuple[str, dict, dict]] = []  # (url, params, kwargs)
        self.post_calls: list[tuple[str, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None, **kwargs):
        self.calls.append((url, params, kwargs))
        return _FakeResponse(self._payload, self._status_code)

    async def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        return _FakeResponse(self._payload, self._status_code)


async def _run(payload, *, platform="reddit", query="q", status_code=200, **extra):
    """Run the raw ``_execute`` body with the network layer stubbed out."""
    fake = _FakeAsyncClient(payload, status_code)
    with mock.patch.object(mod.httpx, "AsyncClient", lambda **kw: fake):
        args = {"query": query, "platform": platform, **extra}
        result = await mod._execute(args, exec=None)
    return result, fake


@pytest.fixture(autouse=True)
def _no_reddit_creds():
    """Keep the anonymous-reddit tests deterministic: no OAuth creds, cold token cache."""
    with mock.patch.dict(
        os.environ,
        {
            "REDDIT_CLIENT_ID": "",
            "REDDIT_CLIENT_SECRET": "",
            "REDDIT_USERNAME": "",
            "REDDIT_PASSWORD": "",
        },
        clear=False,
    ):
        mod._TOKEN_CACHE.clear()
        yield
        mod._TOKEN_CACHE.clear()


# ── plugin contract ──────────────────────────────────────────────────────────
class TestPluginContract:
    def test_tool_name_and_permissions(self):
        tool = mod.PLUGIN.tools[0]
        assert tool.name == "search_social"
        assert tool.permission == {ToolPermission.READ, ToolPermission.NETWORK}
        assert tool.is_concurrency_safe is True

    def test_platform_enum(self):
        props = mod.PLUGIN.tools[0].parameters["properties"]
        assert props["platform"]["enum"] == ["reddit", "x", "zhihu", "auto"]


# ── reddit adapter ───────────────────────────────────────────────────────────
class TestReddit:
    async def test_normalizes_to_unified_schema(self):
        result, fake = await _run(_REDDIT_PAYLOAD)
        assert len(fake.calls) == 1
        assert result == [
            {
                "platform": "reddit",
                "title": "Great thread",
                "content": "x" * mod._SNIPPET_CHARS,  # truncated to the snippet cap
                "author": "alice",
                "metrics": {"score": 42, "comments": 7},
                "url": "https://www.reddit.com/r/test/comments/abc/great_thread/",
                "published_utc": 1234567890,
            }
        ]

    async def test_subreddit_scopes_the_url(self):
        _, fake = await _run(_REDDIT_PAYLOAD, subreddit="MachineLearning")
        url, params, _ = fake.calls[0]
        assert "/r/MachineLearning/search.json" in url
        assert params["q"] == "q"
        assert params["limit"] == 10
        assert params["sort"] == "relevance"

    async def test_absolute_url_left_untouched(self):
        payload = {
            "data": {
                "children": [{"data": {"title": "t", "url": "https://old.reddit.com/x"}}]
            }
        }
        result, _ = await _run(payload)
        assert result[0]["url"] == "https://old.reddit.com/x"

    async def test_limit_is_clamped_to_the_adapter_cap(self):
        _, fake = await _run(_REDDIT_PAYLOAD, limit=999)
        assert fake.calls[0][1]["limit"] == mod._MAX_LIMIT
        _, fake = await _run(_REDDIT_PAYLOAD, limit=0)
        assert fake.calls[0][1]["limit"] == 1


# ── x adapter ────────────────────────────────────────────────────────────────
class TestX:
    async def test_normalizes_tweets(self):
        with mock.patch.dict(os.environ, {"X_BEARER_TOKEN": "t"}, clear=False):
            result, _ = await _run(_X_PAYLOAD, platform="x")
        assert result == [
            {
                "platform": "x",
                "title": "hello",
                "content": "hello",
                "author": "bob",
                "metrics": {"likes": 3, "retweets": 1, "replies": 0},
                "url": "https://x.com/i/web/status/1",
                "published_utc": "2026-01-01T00:00:00Z",
            }
        ]

    async def test_requires_token(self):
        with mock.patch.dict(os.environ, {"X_BEARER_TOKEN": ""}, clear=False):
            with pytest.raises(RuntimeError, match="not configured"):
                await mod._execute({"query": "q", "platform": "x"}, exec=None)


# ── unsupported / bad input ──────────────────────────────────────────────────
class TestUnsupported:
    async def test_zhihu_unsupported(self):
        with pytest.raises(RuntimeError, match="not supported"):
            await mod._execute({"query": "q", "platform": "zhihu"}, exec=None)

    async def test_unknown_platform(self):
        with pytest.raises(RuntimeError, match="unknown platform"):
            await mod._execute({"query": "q", "platform": "myspace"}, exec=None)


# ── HTTP error mapping ───────────────────────────────────────────────────────
class TestHttpErrors:
    async def test_403_maps_to_denied_error(self):
        with pytest.raises(RuntimeError, match="denied"):
            await _run(_REDDIT_PAYLOAD, status_code=403)

    async def test_429_maps_to_rate_limit_error(self):
        with pytest.raises(RuntimeError, match="rate-limited"):
            await _run(_REDDIT_PAYLOAD, status_code=429)

    async def test_5xx_maps_to_status_error(self):
        with pytest.raises(RuntimeError, match="HTTP 500"):
            await _run(_REDDIT_PAYLOAD, status_code=500)


# ── auto merge policy ────────────────────────────────────────────────────────
class TestAuto:
    async def test_runs_only_configured_platforms(self):
        # no X_BEARER_TOKEN → auto == reddit only
        result, fake = await _run(_REDDIT_PAYLOAD, platform="auto")
        assert len(fake.calls) == 1
        assert result and result[0]["platform"] == "reddit"

    async def test_skips_a_failing_adapter(self):
        async def _boom(query, limit, subreddit):
            raise RuntimeError("x exploded")

        with mock.patch.dict(os.environ, {"X_BEARER_TOKEN": "t"}, clear=False):
            with mock.patch.dict(mod._ADAPTERS, {"x": _boom}):
                result, _ = await _run(_REDDIT_PAYLOAD, platform="auto")
        # the broken x adapter must not take down the whole search
        assert result and all(i["platform"] == "reddit" for i in result)


# ── reddit OAuth (official API) ──────────────────────────────────────────────
class TestRedditOAuth:
    async def _run_oauth(self, **extra):
        """Run a reddit search with the four OAuth creds set and the token stub."""
        fake = _FakeAsyncClient(_REDDIT_PAYLOAD)

        async def _fake_token(client, **kw):
            return "tok"

        with mock.patch.dict(
            os.environ,
            {
                "REDDIT_CLIENT_ID": "cid",
                "REDDIT_CLIENT_SECRET": "sec",
                "REDDIT_USERNAME": "u",
                "REDDIT_PASSWORD": "p",
            },
            clear=False,
        ):
            with mock.patch.object(mod, "_get_reddit_token", _fake_token):
                with mock.patch.object(mod.httpx, "AsyncClient", lambda **kw: fake):
                    args = {"query": "q", "platform": "reddit", **extra}
                    result = await mod._execute(args, exec=None)
        return result, fake

    async def test_switches_to_oauth_api_when_creds_set(self):
        result, fake = await self._run_oauth()
        url, params, kwargs = fake.calls[0]
        assert url == "https://oauth.reddit.com/search"
        assert params["q"] == "q"
        assert kwargs["headers"]["Authorization"] == "Bearer tok"
        assert result and result[0]["platform"] == "reddit"

    async def test_oauth_subreddit_scoped(self):
        _, fake = await self._run_oauth(subreddit="MachineLearning")
        assert fake.calls[0][0] == "https://oauth.reddit.com/r/MachineLearning/search"


class TestRedditToken:
    async def test_exchanges_password_grant_and_caches(self):
        fake = _FakeAsyncClient({"access_token": "tok", "expires_in": 86400})
        tok = await mod._get_reddit_token(
            fake, client_id="cid", client_secret="sec", username="u", password="p"
        )
        assert tok == "tok"
        assert "cid" in mod._TOKEN_CACHE
        # a second call reuses the cache without a new token request
        fresh = _FakeAsyncClient({"access_token": "tok", "expires_in": 86400})
        again = await mod._get_reddit_token(
            fresh, client_id="cid", client_secret="sec", username="u", password="p"
        )
        assert again == "tok"
        assert fresh.post_calls == []

    async def test_bad_creds_hint(self):
        fake = _FakeAsyncClient(None, status_code=401)
        with pytest.raises(RuntimeError, match="REDDIT_CLIENT_ID"):
            await mod._get_reddit_token(
                fake, client_id="cid", client_secret="sec", username="u", password="p"
            )

    async def test_missing_access_token_in_response(self):
        fake = _FakeAsyncClient({}, status_code=200)
        with pytest.raises(RuntimeError, match="access_token"):
            await mod._get_reddit_token(
                fake, client_id="cid", client_secret="sec", username="u", password="p"
            )


# ── settings → os.environ secret bridge (deps._agent calls export_secret_env) ─
class TestSecretEnv:
    def test_bridges_settings_into_os_environ(self, monkeypatch):
        for key in ("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "REDDIT_USERNAME", "REDDIT_PASSWORD"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setattr(settings, "reddit_client_id", "cid")
        monkeypatch.setattr(settings, "reddit_client_secret", "sec")
        monkeypatch.setattr(settings, "reddit_username", "u")
        monkeypatch.setattr(settings, "reddit_password", "p")
        export_secret_env()
        assert os.environ["REDDIT_CLIENT_ID"] == "cid"
        assert os.environ["REDDIT_PASSWORD"] == "p"

    def test_existing_env_var_wins(self, monkeypatch):
        monkeypatch.setenv("REDDIT_CLIENT_ID", "direct")
        monkeypatch.setattr(settings, "reddit_client_id", "from-dotenv")
        export_secret_env()
        assert os.environ["REDDIT_CLIENT_ID"] == "direct"

    def test_empty_settings_value_sets_nothing(self, monkeypatch):
        monkeypatch.delenv("REDDIT_USERNAME", raising=False)
        monkeypatch.setattr(settings, "reddit_username", "")
        export_secret_env()
        assert "REDDIT_USERNAME" not in os.environ
