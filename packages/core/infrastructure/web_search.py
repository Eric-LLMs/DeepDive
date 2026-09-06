"""Web search providers, selected by the ``web_search_provider`` config string.

Every provider exposes the same seam: ``search(query, top_k, *, include_domains=None)``
which returns an **outcome envelope** — ``{"status": "ok"|"degraded", "provider": ...,
"results": [...], "error": ...}``. The envelope is the contract: a normal success (with
or without hits) is ``ok``; a provider outage (timeout / network error / auth failure /
HTTP 5xx) is ``degraded`` and is **never** disguised as an empty ``ok`` result, so
Progressive gate diagnostics can tell "no evidence exists" apart from "search infra is
down". Error messages never contain the API key.

Provider name is free text (case-insensitive); supported values:
- ``tavily`` — AI-oriented keyed provider (the production default when
  ``WEB_SEARCH_PROVIDER=tavily``). Always called at ``search_depth="basic"`` with
  ``include_raw_content=False`` / ``include_answer=False`` / ``auto_parameters=False``
  (no silent advanced-tier or answer cost). Domain filtering is native via
  ``include_domains``. DuckDuckGo is attempted **only** after a Tavily failure, as a
  last-resort best-effort fallback tagged ``provider="ddgs"`` — never on the happy path,
  never merged/scored, and if it also fails the outcome stays ``degraded``.
- ``aggregate`` / ``keyless`` — no API key: concurrent Bing + DDG + best-effort Google
  and Baidu scrape, tolerant of any single engine being blocked (the code default).
- ``duckduckgo`` / ``ddg`` — free, no API key required.
- ``bing`` — Bing Web Search API v7, needs an Azure subscription key.
- ``google`` — Google Custom Search JSON API, needs an API key + engine id (cx).

``domain_search(query, top_k, *, domains)`` is the same seam scoped to one or more hosts
(e.g. ``zhihu.com`` / ``x.com``) — used by the ``search_social`` degrade path. Semantically
this is *indexed web / domain search* (asking an engine for pages on a host), **not** an
official platform API. Tavily scopes server-side with ``include_domains``; every other
provider has no native filter, so the keyless site-scoped aggregate path is used instead.

Heavy SDKs are imported lazily so the module stays importable without them; Bing and
Google are called with the stdlib (``urllib``) so no extra dependency is needed.
"""
from __future__ import annotations

import inspect
import math
from urllib.parse import urlparse

from core.config import get_tool_config, settings


# ── outcome envelope (the provider contract) ─────────────────────────────────
def ok_result(provider: str, results: list[dict]) -> dict:
    """Successful outcome. ``results`` may legitimately be empty (no hits)."""
    return {"status": "ok", "provider": provider, "results": list(results)}


def degraded_result(provider: str, error_type: str, message: str) -> dict:
    """Provider fault outcome — never disguises an outage as an empty ``ok`` result."""
    return {
        "status": "degraded",
        "provider": provider,
        "results": [],
        "error": {"type": error_type, "message": message},
    }


# Only these infra faults are worth a best-effort keyless retry. Auth / rate-limit /
# config faults are deterministic and return ``degraded`` immediately.
_TRANSIENT_TYPES = {"timeout", "network", "http_error"}
# Fixed words we probe the exception *text* for to tell auth / rate faults apart from a
# genuine empty result. Only membership is checked — the text itself is never echoed, so a
# provider error message that embeds a secret can not leak into logs or tool errors.
_AUTH_HINTS = ("unauthorized", "invalid api key", "invalidapikey", "authentication", "apikey")
_RATE_HINTS = ("rate limit", "rate-limit", "to many requests", "429")


def classify_search_error(exc: Exception) -> tuple[str, str]:
    """Map an exception to ``(error_type, safe_message)``.

    The message is built from the exception *class* and, when present, an HTTP status code
    — never from the exception *text*. Some SDKs (e.g. ``tavily.errors.InvalidAPIKeyError``)
    expose neither a ``.response`` nor a status code, only an error *class* whose text says
    "invalid api key / unauthorized"; those are detected via the fixed ``_AUTH_HINTS`` /
    ``_RATE_HINTS`` words so a bad key degrades as ``auth``, not ``unknown``. Because the
    raw text is never copied into the returned message, a transport/URL/header detail that
    could carry an API key can not leak.
    """
    code = None
    response = getattr(exc, "response", None)
    if response is not None:
        code = getattr(response, "status_code", None)
    if code is None:
        code = getattr(exc, "code", None) or getattr(exc, "status_code", None) or getattr(exc, "status", None)
    name = type(exc).__name__
    if code is not None:
        if code in (401, 403):
            return "auth", f"authentication failed (HTTP {code}); check the provider API key"
        if code == 429:
            return "rate_limit", "rate limited (HTTP 429); retry later"
        if code >= 500:
            return "http_error", f"provider error (HTTP {code})"
        return "http_error", f"provider rejected the request (HTTP {code})"
    low = name.lower()
    if isinstance(exc, TimeoutError) or "timeout" in low:
        return "timeout", "request timed out"
    if isinstance(exc, (ConnectionError, OSError)) or "connection" in low or "network" in low:
        return "network", "network error reaching the provider"
    text = str(exc).lower()
    if any(h in low or h in text for h in _AUTH_HINTS):
        return "auth", "authentication failed (invalid API key); check the provider API key"
    if any(h in text for h in _RATE_HINTS):
        return "rate_limit", "rate limited; retry later"
    return "unknown", f"unexpected provider error ({name})"


def _ddg_search(query: str, top_k: int) -> list[dict]:
    """Keyless DuckDuckGo results; raises on transport failure.

    Deliberately not a default or merged engine: DDG is only ever tried as a last-resort
    fallback after a configured keyed provider (Tavily) fails, so it can never mask a
    provider outage as "0 results".
    """
    from ddgs import DDGS

    out: list[dict] = []
    with DDGS() as ddgs:
        for r in ddgs.text(query, max_results=max(top_k, 1)):
            title = (r.get("title") or "").strip()
            url = (r.get("href") or "").strip()
            if title and url:
                out.append({"title": title, "url": url, "snippet": (r.get("body") or "").strip()})
    return out


def _host_on(host: str, domains: list[str]) -> bool:
    """True when ``host`` equals a listed domain or is one of its subdomains."""
    host = host.lower()
    return any(host == d or host.endswith("." + d) for d in domains)


class TavilySearch:
    """Tavily Search API adapter — the keyed production provider.

    Defaults keep credit/latency in check: ``search_depth="basic"`` (never the advanced +
    raw_content tier), ``include_raw_content=False``, ``include_answer=False`` and
    ``auto_parameters=False`` (when the installed SDK accepts it), so a call cannot
    silently escalate or add answer cost. ``include_domains`` gives native domain scoping
    for the ``search_social`` degrade path (zhihu / x) — *indexed web/domain search*.

    Every call returns the outcome envelope; on a transient Tavily fault, DuckDuckGo is
    tried once as a last-resort best-effort fallback tagged ``provider="ddgs"`` (its own
    failure is invisible — the reported error is Tavily's, the honest cause).
    """

    name = "tavily"

    def __init__(self, api_key: str, *, search_depth: str = "basic"):
        self._api_key = (api_key or "").strip()
        self._search_depth = search_depth or "basic"
        self._client = None
        self._accepted: set[str] | None = None

    def _get_client(self):
        if self._client is None:
            from tavily import TavilyClient

            self._client = TavilyClient(api_key=self._api_key)
        return self._client

    def _invoke(self, client, query: str, top_k: int, include_domains) -> dict:
        if self._accepted is None:
            self._accepted = set(inspect.signature(client.search).parameters)
        kwargs = {
            "query": query,
            "max_results": max(1, min(int(top_k), 20)),
            "search_depth": self._search_depth,
            "include_raw_content": False,
            "include_answer": False,
        }
        if include_domains:
            kwargs["include_domains"] = list(include_domains)
        if "auto_parameters" in self._accepted:
            kwargs["auto_parameters"] = False  # no implicit advanced-tier escalation
        return client.search(**kwargs)

    def search(self, query: str, top_k: int = 5, *, include_domains=None) -> dict:
        if not self._api_key:
            return degraded_result(self.name, "auth", "WEB_SEARCH_API_KEY is not set")
        try:
            raw = self._invoke(self._get_client(), query, top_k, include_domains)
        except Exception as exc:  # noqa: BLE001 - any infra fault → honest degraded
            error_type, message = classify_search_error(exc)
            if error_type in _TRANSIENT_TYPES:
                try:
                    fallback = _ddg_search(query, top_k)
                except Exception:  # noqa: BLE001 - fallback failure stays invisible
                    fallback = []
                if fallback:
                    return ok_result("ddgs", fallback)
            return degraded_result(self.name, error_type, message)
        results = [
            {"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("content", "")}
            for r in (raw.get("results") or [])
        ]
        return ok_result(self.name, results)


class DuckDuckGoSearch:
    """Keyless DuckDuckGo as a *standalone* provider (not the fallback helper above)."""

    name = "duckduckgo"

    def search(self, query: str, top_k: int = 5, *, include_domains=None) -> dict:
        try:
            results = _ddg_search(query, top_k)
        except Exception as exc:  # noqa: BLE001
            error_type, message = classify_search_error(exc)
            return degraded_result(self.name, error_type, message)
        return ok_result(self.name, results)


class BingSearch:
    """Bing Web Search API v7 — needs an Azure subscription key (``Ocp-Apim-Subscription-Key``)."""

    name = "bing"

    def __init__(self, api_key: str):
        self._api_key = api_key

    def search(self, query: str, top_k: int = 5, *, include_domains=None) -> dict:
        if not self._api_key:
            return degraded_result(self.name, "auth", "WEB_SEARCH_API_KEY (Bing key) is not set")
        import json
        import urllib.parse
        import urllib.request

        url = f"https://api.bing.microsoft.com/v7.0/search?q={urllib.parse.quote(query)}&count={int(top_k)}"
        req = urllib.request.Request(
            url,
            headers={"Ocp-Apim-Subscription-Key": self._api_key},
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.load(resp)
        except Exception as exc:  # noqa: BLE001
            error_type, message = classify_search_error(exc)
            return degraded_result(self.name, error_type, message)
        results = [
            {"title": r.get("name", ""), "url": r.get("url", ""), "snippet": r.get("snippet", "")}
            for r in data.get("webPages", {}).get("value", [])
        ]
        return ok_result(self.name, results)


class AggregateSearch:
    """Keyless multi-engine aggregate (Bing / DDG / Google / Baidu HTML scrape).

    The no-API-key default: runs several free engines concurrently and tolerates each
    one failing on its own (a blocked engine contributes nothing; the survivors still
    return results). See ``core.infrastructure.web_search_aggregate`` for the engine
    details. Optional per-call overrides let callers restrict engines.
    """

    name = "aggregate"

    def __init__(self, engines: tuple[str, ...] | None = None):
        from core.infrastructure import web_search_aggregate

        self._mod = web_search_aggregate
        self._engines = engines

    def search(self, query: str, top_k: int = 5, *, include_domains=None) -> dict:
        try:
            results = self._mod.aggregate_web_search(query, top_k, engines=self._engines)
        except Exception as exc:  # noqa: BLE001
            error_type, message = classify_search_error(exc)
            return degraded_result(self.name, error_type, message)
        return ok_result(self.name, results)


class GoogleSearch:
    """Google Custom Search JSON API — needs an API key + Search Engine ID (cx)."""

    name = "google"

    def __init__(self, api_key: str, engine_id: str):
        self._api_key = api_key
        self._engine_id = engine_id

    def search(self, query: str, top_k: int = 5, *, include_domains=None) -> dict:
        if not self._api_key:
            return degraded_result(self.name, "auth", "WEB_SEARCH_API_KEY (Google key) is not set")
        if not self._engine_id:
            return degraded_result(self.name, "auth", "WEB_SEARCH_ENGINE_ID is not set")
        import json
        import urllib.parse
        import urllib.request

        url = (
            "https://www.googleapis.com/customsearch/v1?"
            f"key={urllib.parse.quote(self._api_key)}&cx={urllib.parse.quote(self._engine_id)}"
            f"&q={urllib.parse.quote(query)}&num={int(top_k)}"
        )
        try:
            with urllib.request.urlopen(url, timeout=15) as resp:
                data = json.load(resp)
        except Exception as exc:  # noqa: BLE001
            error_type, message = classify_search_error(exc)
            return degraded_result(self.name, error_type, message)
        results = [
            {"title": r.get("title", ""), "url": r.get("link", ""), "snippet": r.get("snippet", "")}
            for r in data.get("items", [])
        ]
        return ok_result(self.name, results)


def _provider_name() -> str:
    """The configured provider name (from the tools namespace, else flat settings)."""
    tc = get_tool_config("web_search")
    return (tc.get("provider") or settings.web_search_provider or "").strip().lower()


def build_web_search_provider(name: str, *, api_key: str = "", engine_id: str = ""):
    """Construct a provider object by name with already-resolved credentials.

    The single name→class map shared by runtime resolution (``get_web_search_provider``)
    and the admin-console connectivity test (``POST /config/test-web-search``). Callers
    resolve the credentials first (stored tools config, then flat settings fallback);
    this function never reads config itself. Provider name is case-insensitive; an
    unknown name raises a RuntimeError listing the supported values so a typo is obvious.
    """
    name = (name or "").strip().lower()
    if name in ("aggregate", "keyless"):
        return AggregateSearch()
    if name in ("duckduckgo", "ddg"):
        return DuckDuckGoSearch()
    if name == "tavily":
        return TavilySearch(api_key or "")
    if name == "bing":
        return BingSearch(api_key or "")
    if name == "google":
        return GoogleSearch(api_key or "", engine_id or "")
    raise RuntimeError(
        f"unknown web search provider: {name!r}; "
        "supported: duckduckgo, tavily, bing, google, aggregate"
    )


def get_web_search_provider():
    """Return a provider object with ``search(query, top_k, *, include_domains=None)``.

    Credentials are read DB-first: the generic tools namespace mirrored from the stored
    admin config (``get_tool_config("web_search")``) wins; flat settings keys (env /
    config file) are only the fallback when the DB has nothing for a field.
    """
    tc = get_tool_config("web_search")
    api_key = tc.get("api_key") or settings.web_search_api_key
    engine_id = tc.get("engine_id")
    if engine_id is None:
        engine_id = settings.web_search_engine_id
    return build_web_search_provider(
        _provider_name(), api_key=api_key or "", engine_id=engine_id or ""
    )


def domain_search(query: str, top_k: int = 6, *, domains, provider=None, name=None) -> dict:
    """Domain-scoped web search through the provider seam (outcome envelope).

    Used by the ``search_social`` degrade path for platforms with no public API (e.g.
    zhihu / x). Semantic note: this is *indexed web / domain search* — it asks the search
    backend for pages living on a host, it is **not** an official platform API.

    - ``tavily`` scopes the query server-side with ``include_domains`` (native filter).
    - Every other provider has no server-side domain filter, so the keyless site-scoped
      aggregate path is used instead (``site:`` per host plus a plain-query fallback), and
      hits are post-filtered to the requested hosts.

    Either way every returned URL lives on one of ``domains`` (host or subdomain). An
    outage / timeout / auth failure is returned as ``degraded`` — never an empty ``ok``.
    """
    domains = [d.strip().lower().lstrip("*.") for d in (domains or []) if d and d.strip()]
    if not domains or not query.strip():
        return ok_result("domain", [])
    name = (name or _provider_name()).lower()
    if name == "tavily":
        prov = provider or get_web_search_provider()
        outcome = prov.search(query, top_k, include_domains=domains)
        if outcome.get("status") != "ok":
            return outcome  # honest degraded — infra fault, not an empty result
        kept = [
            hit for hit in outcome.get("results", []) if _host_on(urlparse(hit.get("url", "")).hostname or "", domains)
        ][: max(top_k, 0)]
        return ok_result("tavily", kept)
    # Non-tavily providers cannot scope server-side → per-host site-scoped aggregate.
    from core.infrastructure import web_search_aggregate as agg

    merged: list[dict] = []
    seen: set[tuple[str, str]] = set()
    per_host = max(1, math.ceil(max(top_k, 0) / len(domains)))
    try:
        for host in domains:
            for hit in agg.site_limited_web_search(host, query, top_k=per_host):
                h = (urlparse(hit.get("url", "")).hostname or "").lower()
                if not _host_on(h, domains):
                    continue
                key = (h, hit.get("title", "").strip().lower())
                if key in seen:
                    continue
                seen.add(key)
                merged.append(hit)
                if len(merged) >= max(top_k, 0):
                    return ok_result("aggregate", merged)
    except Exception as exc:  # noqa: BLE001
        error_type, message = classify_search_error(exc)
        return degraded_result("aggregate", error_type, message)
    return ok_result("aggregate", merged)
