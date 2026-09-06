"""Deterministic (offline) tests for the web-search provider outcome envelope.

The seam contract: every provider returns ``{"status": "ok"|"degraded", "provider",
"results", "error"}`` — a provider outage (timeout / auth / 5xx) is ``degraded``, never a
disguised empty ``ok``. Tavily is the keyed production provider: basic depth by default,
no raw-content / answer / auto-advanced, native ``include_domains`` for the zhihu/x domain
degrade, and DuckDuckGo as a last-resort fallback only behind a Tavily failure.
"""
from types import SimpleNamespace
from unittest import mock

from core.infrastructure import web_search as ws
from core.infrastructure import web_search_aggregate as agg
from core.infrastructure.web_search import (
    AggregateSearch,
    BingSearch,
    DuckDuckGoSearch,
    GoogleSearch,
    TavilySearch,
    build_web_search_provider,
    classify_search_error,
    degraded_result,
    domain_search,
    ok_result,
)


# ── build_web_search_provider (the name → class map, shared w/ the admin test) ─
class TestBuildProvider:
    def test_maps_supported_names_case_insensitively(self):
        assert isinstance(build_web_search_provider("tavily", api_key="k"), TavilySearch)
        assert isinstance(build_web_search_provider("TAVILY", api_key="k"), TavilySearch)
        assert isinstance(build_web_search_provider("duckduckgo"), DuckDuckGoSearch)
        assert isinstance(build_web_search_provider("ddg"), DuckDuckGoSearch)
        assert isinstance(build_web_search_provider("bing", api_key="k"), BingSearch)
        assert isinstance(build_web_search_provider("aggregate"), AggregateSearch)
        assert isinstance(build_web_search_provider("keyless"), AggregateSearch)
        assert isinstance(
            build_web_search_provider("google", api_key="k", engine_id="cx"), GoogleSearch
        )

    def test_unknown_name_raises(self):
        import pytest

        with pytest.raises(RuntimeError, match="unknown web search provider"):
            build_web_search_provider("myspace")

    def test_blank_name_raises(self):
        import pytest

        with pytest.raises(RuntimeError, match="unknown web search provider"):
            build_web_search_provider("")


# ── envelope shape ───────────────────────────────────────────────────────────
class TestEnvelope:
    def test_ok_shape(self):
        out = ok_result("tavily", [{"title": "a", "url": "https://a.com/", "snippet": ""}])
        assert out == {
            "status": "ok",
            "provider": "tavily",
            "results": [{"title": "a", "url": "https://a.com/", "snippet": ""}],
        }

    def test_ok_empty_is_still_ok(self):
        out = ok_result("tavily", [])
        assert out["status"] == "ok"
        assert out["results"] == []

    def test_degraded_shape(self):
        out = degraded_result("tavily", "auth", "check the key")
        assert out == {
            "status": "degraded",
            "provider": "tavily",
            "results": [],
            "error": {"type": "auth", "message": "check the key"},
        }


# ── error classification never leaks secrets ─────────────────────────────────
class TestClassify:
    def test_http_status_mapping(self):
        assert classify_search_error(SimpleNamespace(response=SimpleNamespace(status_code=401)))[0] == "auth"
        assert classify_search_error(SimpleNamespace(response=SimpleNamespace(status_code=403)))[0] == "auth"
        assert classify_search_error(SimpleNamespace(response=SimpleNamespace(status_code=429)))[0] == "rate_limit"
        assert classify_search_error(SimpleNamespace(response=SimpleNamespace(status_code=503)))[0] == "http_error"
        assert classify_search_error(SimpleNamespace(code=500))[0] == "http_error"

    def test_timeout_and_network(self):
        assert classify_search_error(TimeoutError("slow"))[0] == "timeout"
        assert classify_search_error(ConnectionError("refused"))[0] == "network"

    def test_sdk_invalid_key_error_is_auth(self):
        # tavily.errors.InvalidAPIKeyError exposes no .response/status_code — only its
        # class + message. It must classify as auth, never as an opaque "unknown".
        class _InvalidApiKey(Exception):
            def __str__(self):
                return "Invalid API key provided — Unauthorized"

        error_type, message = classify_search_error(_InvalidApiKey())
        assert error_type == "auth"
        assert message == "authentication failed (invalid API key); check the provider API key"

    def test_message_is_built_from_class_not_str(self):
        # A transport exception whose __str__ embeds a secret must never leak it: the
        # message is constructed from the exception class name, not str(exc).
        class _Leaky(Exception):
            def __str__(self):
                return "WEB_SEARCH_API_KEY=tvly-abcdef-leak"

        error_type, message = classify_search_error(_Leaky())
        assert error_type == "unknown"
        assert "tvly-abcdef" not in message
        assert "WEB_SEARCH_API_KEY=" not in message


# ── TavilySearch (no network) ────────────────────────────────────────────────
class _FakeTavilyClient:
    def __init__(self, payload=None, exc=None):
        self._payload = payload
        self._exc = exc
        self.calls: list[dict] = []

    def search(self, query, max_results=5, search_depth="basic", include_raw_content=False,
               include_answer=False, include_domains=None, auto_parameters=False):
        self.calls.append({
            "query": query,
            "max_results": max_results,
            "search_depth": search_depth,
            "include_raw_content": include_raw_content,
            "include_answer": include_answer,
            "include_domains": include_domains,
            "auto_parameters": auto_parameters,
        })
        if self._exc is not None:
            raise self._exc
        return self._payload or {"results": []}


def _tavily_with(fake) -> TavilySearch:
    prov = TavilySearch("tvly-test-key")
    prov._client = fake  # bypass the lazy SDK import; nothing hits the network
    return prov


class TestTavilySearch:
    def test_missing_key_is_degraded_auth(self):
        out = TavilySearch("").search("q")
        assert out["status"] == "degraded"
        assert out["error"]["type"] == "auth"

    def test_happy_path_maps_results_and_stays_basic(self):
        fake = _FakeTavilyClient({
            "results": [
                {"title": "T", "url": "https://example.com/", "content": "snippet text"},
            ]
        })
        out = _tavily_with(fake).search("tomato", top_k=4)
        assert out["status"] == "ok"
        assert out["provider"] == "tavily"
        assert out["results"][0] == {"title": "T", "url": "https://example.com/", "snippet": "snippet text"}
        sent = fake.calls[0]
        # credit/latency controls: basic depth, no advanced + raw_content, no answer.
        assert sent["search_depth"] == "basic"
        assert sent["include_raw_content"] is False
        assert sent["include_answer"] is False
        assert sent["auto_parameters"] is False
        assert sent["max_results"] == 4

    def test_include_domains_passes_through(self):
        fake = _FakeTavilyClient({"results": []})
        _tavily_with(fake).search("q", top_k=5, include_domains=["zhihu.com"])
        assert fake.calls[0]["include_domains"] == ["zhihu.com"]

    def test_max_results_capped(self):
        fake = _FakeTavilyClient({"results": []})
        _tavily_with(fake).search("q", top_k=999)
        assert fake.calls[0]["max_results"] == 20

    def test_transient_fault_falls_back_to_ddgs(self):
        # Tavily down (timeout) → last-resort DDGS best-effort; its hits are tagged ddgs.
        fake = _FakeTavilyClient(exc=TimeoutError("slow"))
        with mock.patch.object(ws, "_ddg_search", return_value=[
            {"title": "d", "url": "https://ddg.example/", "snippet": ""}
        ]):
            out = _tavily_with(fake).search("q")
        assert out["status"] == "ok"
        assert out["provider"] == "ddgs"

    def test_all_failed_is_degraded_not_ok_empty(self):
        # Both Tavily and the DDGS fallback down → degraded with the honest cause (Tavily).
        fake = _FakeTavilyClient(exc=TimeoutError("slow"))
        with mock.patch.object(ws, "_ddg_search", side_effect=TimeoutError("ddg down")):
            out = _tavily_with(fake).search("q")
        assert out["status"] == "degraded"
        assert out["provider"] == "tavily"
        assert out["error"]["type"] == "timeout"
        assert out["results"] == []

    def test_auth_fault_does_not_attempt_ddgs(self):
        # Deterministic auth fault → degraded immediately (no futile keyless retry).
        class _Http401(Exception):
            def __init__(self):
                super().__init__("401")
                self.response = SimpleNamespace(status_code=401)

        fake = _FakeTavilyClient(exc=_Http401())
        with mock.patch.object(ws, "_ddg_search") as ddg:
            out = _tavily_with(fake).search("q")
        assert out["status"] == "degraded"
        assert out["error"]["type"] == "auth"
        ddg.assert_not_called()


# ── domain_search (the search_social zhihu / x degrade seam) ─────────────────
class TestDomainSearch:
    def test_empty_domains_is_ok_empty(self):
        out = domain_search("q", domains=[])
        assert out["status"] == "ok"
        assert out["results"] == []

    def test_tavily_branch_filters_to_hosts(self):
        provider = mock.Mock()
        provider.search.return_value = ok_result("tavily", [
            {"title": "a", "url": "https://www.zhihu.com/question/1", "snippet": ""},
            {"title": "z", "url": "https://zhuanlan.zhihu.com/p/2", "snippet": ""},
            {"title": "off", "url": "https://evil.example/x", "snippet": ""},
        ])
        out = domain_search("q", top_k=5, domains=["zhihu.com"], provider=provider, name="tavily")
        assert out["provider"] == "tavily"
        assert [h["url"] for h in out["results"]] == [
            "https://www.zhihu.com/question/1",
            "https://zhuanlan.zhihu.com/p/2",
        ]
        # the domain constraint was sent to the engine as include_domains
        assert provider.search.call_args.kwargs["include_domains"] == ["zhihu.com"]

    def test_tavily_branch_propagates_degraded(self):
        provider = mock.Mock()
        provider.search.return_value = degraded_result("tavily", "timeout", "request timed out")
        out = domain_search("q", domains=["zhihu.com"], provider=provider, name="tavily")
        assert out["status"] == "degraded"
        assert out["error"]["type"] == "timeout"

    def test_non_tavily_branch_uses_site_scoped_aggregate(self):
        # Providers without a native domain filter fall back to the keyless site-scoped
        # aggregate; hits are post-filtered and deduped across hosts.
        by_host = {
            "x.com": [
                {"title": "x1", "url": "https://x.com/a/1", "snippet": ""},
                {"title": "off", "url": "https://evil.example/o", "snippet": ""},
            ],
            "twitter.com": [
                {"title": "t1", "url": "https://twitter.com/b/1", "snippet": ""},
                {"title": "x1", "url": "https://x.com/a/1", "snippet": ""},  # duplicate
            ],
        }

        def _fake_site(site, query, top_k=6, *, timeout=7.0):
            return by_host.get(site, [])

        with mock.patch.object(agg, "site_limited_web_search", side_effect=_fake_site) as fake:
            out = domain_search("q", top_k=5, domains=["x.com", "twitter.com"], name="duckduckgo")
        assert out["status"] == "ok"
        assert out["provider"] == "aggregate"
        assert [h["url"] for h in out["results"]] == ["https://x.com/a/1", "https://twitter.com/b/1"]
        assert [c.args[0] for c in fake.call_args_list] == ["x.com", "twitter.com"]
