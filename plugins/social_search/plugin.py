"""``social_search``: one tool, many social platforms (adapter pattern).

``search_social`` takes a query and a target platform and returns a unified list of
structured items, so skills and the model handle every source identically — the output
schema never changes, only the ``platform`` tag on each item.

Platform reality (honest):
- ``reddit`` — anonymous ``www.reddit.com/search.json`` first, but many IPs (datacenter
  / VPN exits) get 403, and ``old.reddit.com`` now redirects anonymous JSON to login.
  When the four OAuth env vars are set (``REDDIT_CLIENT_ID``, ``REDDIT_CLIENT_SECRET``,
  ``REDDIT_USERNAME``, ``REDDIT_PASSWORD`` — a free *script* app from
  https://www.reddit.com/prefs/apps), the adapter transparently switches to the official
  ``oauth.reddit.com`` API, which is the reliable live path.
- ``x`` — the free public API is gone. With an ``X_BEARER_TOKEN`` env var set (X API v2
  recent search) the official API is used; without a token, the adapter *degrades* to a
  site-scoped aggregate web search (``site:x.com`` + ``site:twitter.com``) instead of
  failing — search engines still index much of what is posted there.
- ``zhihu`` — no public API and aggressive anti-bot. The adapter degrades to a
  site-scoped aggregate web search (``site:zhihu.com``) so answers and articles remain
  reachable through the engines that index them.

The x / zhihu degrade path runs through the same keyless multi-engine aggregate as the
``web_search`` tool (``core.infrastructure.web_search_aggregate``): concurrent engines,
tolerance degradation, real-URL decode, dedup/fusion/quality filter — a single code path
for "no key, no login, still get results".

``platform="auto"`` runs every adapter that is actually configured (reddit always, x only
when a token exists) concurrently and merges the results.

The tool does not summarize or re-reason: the model running the chat is the LLM and already
holds the full conversation context, so it generates the query and does the
understanding/organizing itself, guided by skills (e.g. ``social_research``).

Permission is ``READ + NETWORK``: the sandbox denies (or asks approval for) this tool when
the session has not been granted network access — the system's own permission gate is the
"auth check", not per-platform OAuth.

Loaded by ``PluginManager.discover`` (``plugins/**/plugin.py``) at startup; no central
registry edit required.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import time
from urllib.parse import quote, urlparse

import httpx
from agent.engine.decisions import text_block
from agent.plugins.base import Plugin
from agent.tools.definition import ToolOutput, define_tool
from agent.tools.tool_permissions import ToolPermission
from core.infrastructure.web_search_aggregate import site_limited_web_search

_USER_AGENT = "deepdive-social-search/0.1 (learning-workbench assistant)"
_TIMEOUT = httpx.Timeout(15.0)
_MAX_LIMIT = 25
_SNIPPET_CHARS = 500

_SUPPORTED = ("reddit", "x", "zhihu", "auto")

# ── reddit OAuth (official API) ──────────────────────────────────────────────
_REDDIT_OAUTH = "https://oauth.reddit.com"
_REDDIT_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
# Bearer tokens last ~24h; cache per client_id so a chat session does not re-request
# a token on every search call.
_TOKEN_CACHE: dict[str, tuple[str, float]] = {}
_TOKEN_CACHE_FRESH = 600  # reuse a cached token for this many seconds (well under 24h)


def _item(platform, *, title, content, url, author="", metrics=None, published_utc=None) -> dict:
    """One normalized item across every platform. This shape is the public contract."""
    return {
        "platform": platform,
        "title": title,
        "content": content,
        "author": author,
        "metrics": metrics or {},
        "url": url,
        "published_utc": published_utc,
    }


def _web_degrade_to_items(platform: str, hosts: list[str], query: str, limit: int) -> list[dict]:
    """Degrade path for platforms with no key/login: site-scoped aggregate web search.

    Runs ``site:<host> <query>`` through the keyless multi-engine aggregate for each
    host, then dedupes and caps to ``limit``. The aggregate already dedupes/filters by
    quality; here we additionally guarantee every returned URL lives on the platform's
    own host, so an off-site hit can never masquerade as social content.
    """
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    per_host = max(1, math.ceil(limit / len(hosts)))
    for host in hosts:
        for hit in site_limited_web_search(host, query, top_k=per_host):
            key = (urlparse(hit["url"]).hostname or "", hit["title"].strip().lower())
            if key in seen:
                continue
            seen.add(key)
            out.append(
                _item(
                    platform,
                    title=hit["title"],
                    content=hit["snippet"][:_SNIPPET_CHARS],
                    url=hit["url"],
                )
            )
            if len(out) >= limit:
                return out
    return out


# ── reddit adapter ───────────────────────────────────────────────────────────
def _reddit_creds() -> bool:
    """True when the four OAuth env vars are all set (a free script app)."""
    return bool(
        os.environ.get("REDDIT_CLIENT_ID")
        and os.environ.get("REDDIT_CLIENT_SECRET")
        and os.environ.get("REDDIT_USERNAME")
        and os.environ.get("REDDIT_PASSWORD")
    )


async def _get_reddit_token(
    client: httpx.AsyncClient,
    *,
    client_id: str,
    client_secret: str,
    username: str,
    password: str,
) -> str:
    """Exchange script-app credentials for a bearer token (password grant).

    Uses the shared ``client`` so the caller's transport/headers apply. The token is
    cached per ``client_id``; a fresh token is fetched only when the cache is cold.
    """
    cached = _TOKEN_CACHE.get(client_id)
    if cached and cached[1] > time.time():
        return cached[0]
    resp = await client.post(
        _REDDIT_TOKEN_URL,
        data={"grant_type": "password", "username": username, "password": password},
        auth=(client_id, client_secret),
        headers={"User-Agent": _USER_AGENT},
    )
    if resp.status_code >= 400:
        code = resp.status_code
        hint = (
            " — check REDDIT_CLIENT_ID/SECRET and the account password"
            if code in (401, 403)
            else ""
        )
        raise RuntimeError(f"reddit OAuth token request failed (HTTP {code}){hint}")
    payload = resp.json()
    token = payload.get("access_token")
    if not token:
        raise RuntimeError("reddit OAuth token response missing access_token")
    _TOKEN_CACHE[client_id] = (token, time.time() + _TOKEN_CACHE_FRESH)
    return token


async def _reddit_oauth(query: str, limit: int, subreddit: str | None) -> list[dict]:
    """Official ``oauth.reddit.com`` search. Same listing shape as the anonymous API."""
    client_id = os.environ.get("REDDIT_CLIENT_ID") or ""
    client_secret = os.environ.get("REDDIT_CLIENT_SECRET") or ""
    username = os.environ.get("REDDIT_USERNAME") or ""
    password = os.environ.get("REDDIT_PASSWORD") or ""
    base = (
        f"{_REDDIT_OAUTH}/r/{quote(subreddit)}/search"
        if subreddit
        else f"{_REDDIT_OAUTH}/search"
    )
    params = {"q": query, "limit": limit, "sort": "relevance", "t": "all"}
    async with httpx.AsyncClient(
        timeout=_TIMEOUT, headers={"User-Agent": _USER_AGENT}, follow_redirects=True
    ) as client:
        token = await _get_reddit_token(
            client,
            client_id=client_id,
            client_secret=client_secret,
            username=username,
            password=password,
        )
        resp = await client.get(base, params=params, headers={"Authorization": f"Bearer {token}"})
        resp.raise_for_status()
        payload = resp.json()
    return _parse_reddit_listing(payload)


def _parse_reddit_listing(payload: dict) -> list[dict]:
    """Normalize a reddit Listing payload (``data.children``) to unified items."""
    out: list[dict] = []
    for post in (payload.get("data") or {}).get("children") or []:
        d = post.get("data") or {}
        url = d.get("url") or ""
        if url.startswith("/r/"):
            url = f"https://www.reddit.com{url}"
        out.append(
            _item(
                "reddit",
                title=d.get("title") or "",
                content=(d.get("selftext") or "")[:_SNIPPET_CHARS],
                author=d.get("author") or "",
                metrics={"score": d.get("score") or 0, "comments": d.get("num_comments") or 0},
                url=url,
                published_utc=d.get("created_utc"),
            )
        )
    return out


async def _reddit(query: str, limit: int, subreddit: str | None) -> list[dict]:
    """Reddit search: official OAuth API when configured, anonymous JSON otherwise.

    The anonymous ``www.reddit.com/search.json`` path is blocked (403) from many
    network exits; configuring the OAuth env vars is the reliable way to go live.
    """
    if _reddit_creds():
        return await _reddit_oauth(query, limit, subreddit)
    base = (
        f"https://www.reddit.com/r/{quote(subreddit)}/search.json"
        if subreddit
        else "https://www.reddit.com/search.json"
    )
    params = {"q": query, "limit": limit, "sort": "relevance", "t": "all"}
    headers = {"User-Agent": _USER_AGENT}
    async with httpx.AsyncClient(timeout=_TIMEOUT, headers=headers, follow_redirects=True) as client:
        resp = await client.get(base, params=params)
        resp.raise_for_status()
        payload = resp.json()
    return _parse_reddit_listing(payload)


# ── x adapter (X API v2 recent search; degrades to site-scoped web without a token) ─
async def _x(query: str, limit: int, subreddit: str | None) -> list[dict]:
    token = os.environ.get("X_BEARER_TOKEN")
    if not token:
        # Degrade, never fail: without an API token, ask the keyless aggregate for
        # x.com / twitter.com pages the engines still index. Runs in a thread because
        # the aggregate does blocking HTTP internally.
        return await asyncio.to_thread(
            _web_degrade_to_items, "x", ["x.com", "twitter.com"], query, limit
        )
    headers = {"Authorization": f"Bearer {token}", "User-Agent": _USER_AGENT}
    params = {
        "query": query,
        "max_results": min(limit, 10),  # free-tier cap
        "tweet.fields": "public_metrics,created_at",
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT, headers=headers, follow_redirects=True) as client:
        resp = await client.get("https://api.twitter.com/2/tweets/search/recent", params=params)
        resp.raise_for_status()
        payload = resp.json()

    out: list[dict] = []
    for tw in payload.get("data") or []:
        m = tw.get("public_metrics") or {}
        out.append(
            _item(
                "x",
                title=tw.get("text", "")[:80],
                content=tw.get("text") or "",
                author=tw.get("author_id") or "",
                metrics={
                    "likes": m.get("like_count") or 0,
                    "retweets": m.get("retweet_count") or 0,
                    "replies": m.get("reply_count") or 0,
                },
                url=f"https://x.com/i/web/status/{tw.get('id')}",
                published_utc=tw.get("created_at"),
            )
        )
    return out


# ── zhihu adapter (no public API; degrades to site-scoped web search) ─────────
async def _zhihu(query: str, limit: int, subreddit: str | None) -> list[dict]:
    return await asyncio.to_thread(
        _web_degrade_to_items, "zhihu", ["zhihu.com"], query, limit
    )


_ADAPTERS = {"reddit": _reddit, "x": _x, "zhihu": _zhihu}


def _available() -> list[str]:
    """Platforms merged by ``auto``: reddit always, plus official-API platforms whose
    keys are configured. x / zhihu degrade paths are *always* available, but running two
    extra site-scoped aggregate searches on every ``auto`` call is slow, so they are
    opt-in via an explicit ``platform`` argument rather than part of ``auto``."""
    available = ["reddit"]
    if os.environ.get("X_BEARER_TOKEN"):
        available.append("x")
    return available


async def _execute(args: dict, exec) -> list[dict]:
    platform = args.get("platform", "reddit").lower()
    if platform not in _SUPPORTED:
        raise RuntimeError(f"unknown platform: {platform!r} (use one of {_SUPPORTED})")
    query = args["query"]
    limit = min(max(int(args.get("limit", 10)), 1), _MAX_LIMIT)
    subreddit = args.get("subreddit")

    if platform == "auto":
        results = await asyncio.gather(
            *[ADAPTER(query, limit, subreddit) for ADAPTER in (_ADAPTERS[p] for p in _available())],
            return_exceptions=True,
        )
        merged: list[dict] = []
        for r in results:
            if isinstance(r, BaseException):
                continue  # a failing platform never takes down the others
            merged.extend(r)
        return merged

    try:
        return await _ADAPTERS[platform](query, limit, subreddit)
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        if code in (429, 403):
            raise RuntimeError(
                f"{platform} rate-limited or denied ({code}); retry later or use a "
                "platform with live configuration"
            ) from exc
        raise RuntimeError(f"{platform} returned HTTP {code}") from exc
    except httpx.HTTPError as exc:
        raise RuntimeError(f"{platform} request failed: {exc}") from exc


PLUGIN = Plugin(
    name="social_search",
    description="Cross-platform social search tool (search_social).",
    tools=[
        define_tool(
            name="search_social",
            description=(
                "Search social sources for a query and return unified structured items "
                "(title, content, author, metrics, url). Platforms: reddit (anonymous "
                "JSON first; REDDIT_CLIENT_ID/SECRET/USERNAME/PASSWORD enables the live "
                "OAuth API), x (X_BEARER_TOKEN enables the official API; otherwise it "
                "degrades to a keyless site-scoped web search of x.com/twitter.com), "
                "zhihu (no public API; degrades to a keyless site-scoped web search of "
                "zhihu.com). Use it to find community opinions, first-hand experiences, "
                "or current discussion on a topic."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query."},
                    "platform": {
                        "type": "string",
                        "enum": ["reddit", "x", "zhihu", "auto"],
                        "description": "Target platform; 'auto' merges every configured platform.",
                    },
                    "subreddit": {
                        "type": "string",
                        "description": 'Optional reddit-only scope, e.g. "MachineLearning".',
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results per platform (1-25, default 10).",
                    },
                },
                "required": ["query"],
            },
            output=ToolOutput(
                schema={"type": "array"},
                render=lambda args, value: [
                    text_block(json.dumps(value, ensure_ascii=False, default=str))
                ],
            ),
            execute=_execute,
            is_concurrency_safe=True,
            permission={ToolPermission.READ, ToolPermission.NETWORK},
        )
    ],
)
