"""Web search providers, selected by the ``web_search_provider`` config string.

Provider name is free text (case-insensitive); supported values:
- ``duckduckgo`` / ``ddg`` — free, no API key required.
- ``tavily`` — AI-oriented, needs ``WEB_SEARCH_API_KEY``.
- ``bing`` — Bing Web Search API v7, needs an Azure subscription key.
- ``google`` — Google Custom Search JSON API, needs an API key + engine id (cx).

Heavy SDKs are imported lazily so the module stays importable without them; Bing and
Google are called with the stdlib (``urllib``) so no extra dependency is needed.
"""
from __future__ import annotations

from core.config import get_tool_config, settings


class TavilySearch:
    def __init__(self, api_key: str):
        self._api_key = api_key
        self._client = None

    def _get_client(self):
        if self._client is None:
            if not self._api_key:
                raise RuntimeError(
                    "web search not configured: set WEB_SEARCH_API_KEY "
                    "(or WEB_SEARCH_PROVIDER=duckduckgo)"
                )
            from tavily import TavilyClient

            self._client = TavilyClient(api_key=self._api_key)
        return self._client

    def search(self, query: str, top_k: int = 5) -> list[dict]:
        resp = self._get_client().search(query, max_results=top_k)
        return [
            {
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "snippet": r.get("content", ""),
            }
            for r in resp.get("results", [])
        ]


class DuckDuckGoSearch:
    def search(self, query: str, top_k: int = 5) -> list[dict]:
        from ddgs import DDGS

        results = []
        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=top_k):
                results.append(
                    {
                        "title": r.get("title", ""),
                        "url": r.get("href", ""),
                        "snippet": r.get("body", ""),
                    }
                )
        return results


class BingSearch:
    """Bing Web Search API v7 — needs an Azure subscription key (``Ocp-Apim-Subscription-Key``)."""

    def __init__(self, api_key: str):
        self._api_key = api_key

    def search(self, query: str, top_k: int = 5) -> list[dict]:
        if not self._api_key:
            raise RuntimeError(
                "web search not configured: set WEB_SEARCH_API_KEY to your "
                "Bing subscription key (Azure) before using provider 'bing'"
            )
        import json
        import urllib.parse
        import urllib.request

        url = f"https://api.bing.microsoft.com/v7.0/search?q={urllib.parse.quote(query)}&count={top_k}"
        req = urllib.request.Request(
            url,
            headers={"Ocp-Apim-Subscription-Key": self._api_key},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.load(resp)
        return [
            {
                "title": r.get("name", ""),
                "url": r.get("url", ""),
                "snippet": r.get("snippet", ""),
            }
            for r in data.get("webPages", {}).get("value", [])
        ]


class GoogleSearch:
    """Google Custom Search JSON API — needs an API key + Search Engine ID (cx)."""

    def __init__(self, api_key: str, engine_id: str):
        self._api_key = api_key
        self._engine_id = engine_id

    def search(self, query: str, top_k: int = 5) -> list[dict]:
        if not self._api_key:
            raise RuntimeError(
                "web search not configured: set WEB_SEARCH_API_KEY to your Google "
                "API key before using provider 'google'"
            )
        if not self._engine_id:
            raise RuntimeError(
                "web search not configured: set WEB_SEARCH_ENGINE_ID (Google Custom "
                "Search engine id, cx) before using provider 'google'"
            )
        import json
        import urllib.parse
        import urllib.request

        url = (
            "https://www.googleapis.com/customsearch/v1?"
            f"key={self._api_key}&cx={self._engine_id}"
            f"&q={urllib.parse.quote(query)}&num={top_k}"
        )
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.load(resp)
        return [
            {
                "title": r.get("title", ""),
                "url": r.get("link", ""),
                "snippet": r.get("snippet", ""),
            }
            for r in data.get("items", [])
        ]


def get_web_search_provider():
    """Return a provider object with a ``search(query, top_k) -> list[dict]`` method.

    Params are read from the generic tools namespace (``get_tool_config("web_search")``),
    falling back to the flat settings keys. Provider name is matched case-insensitively;
    an unknown name raises a RuntimeError listing the supported values so a typo is obvious.
    """
    tc = get_tool_config("web_search")
    name = (tc.get("provider") or settings.web_search_provider or "").strip().lower()
    api_key = tc.get("api_key") or settings.web_search_api_key
    engine_id = tc.get("engine_id")
    if engine_id is None:
        engine_id = settings.web_search_engine_id
    if name in ("duckduckgo", "ddg"):
        return DuckDuckGoSearch()
    if name == "tavily":
        return TavilySearch(api_key)
    if name == "bing":
        return BingSearch(api_key)
    if name == "google":
        return GoogleSearch(api_key, engine_id)
    raise RuntimeError(
        f"unknown web search provider: {name!r}; "
        "supported: duckduckgo, tavily, bing, google"
    )
