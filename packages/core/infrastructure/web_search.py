"""Web search providers, selected by ``WEB_SEARCH_PROVIDER``.

Heavy SDKs are imported lazily so the module stays importable without them:
- Tavily (``tavily-python``) — AI-oriented, needs ``WEB_SEARCH_API_KEY``.
- DuckDuckGo (``ddgs``) — no key required.
"""
from __future__ import annotations

from core.config import settings


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


def get_web_search_provider():
    """Return a provider object with a ``search(query, top_k) -> list[dict]`` method."""
    if settings.web_search_provider.lower() == "duckduckgo":
        return DuckDuckGoSearch()
    return TavilySearch(settings.web_search_api_key)
