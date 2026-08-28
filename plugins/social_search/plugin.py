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
- ``x`` — the free public API is gone; this adapter only works when a ``X_BEARER_TOKEN``
  env var is set (X API v2 recent search). Without it, it raises a clear error.
- ``zhihu`` — no public API and aggressive anti-bot; there is no reliable free path. The
  adapter raises a clear error until a cookie-authenticated scraper is added.

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
import os
import time
from urllib.parse import quote

import httpx

from agent.engine.decisions import text_block
from agent.plugins.base import Plugin
from agent.tools.definition import ToolOutput, define_tool
from agent.tools.tool_permissions import ToolPermission

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


# ── x adapter (X API v2 recent search, needs a bearer token) ────────────────
async def _x(query: str, limit: int, subreddit: str | None) -> list[dict]:
    token = os.environ.get("X_BEARER_TOKEN")
    if not token:
        raise RuntimeError(
            "x is not configured: set X_BEARER_TOKEN (X API v2) to enable the x adapter"
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


# ── zhihu adapter (no public API; blocked without a scraper) ────────────────
async def _zhihu(query: str, limit: int, subreddit: str | None) -> list[dict]:
    raise RuntimeError(
        "zhihu is not supported: it has no public API and blocks scraping; add a "
        "cookie-authenticated scraper to enable the zhihu adapter"
    )


_ADAPTERS = {"reddit": _reddit, "x": _x, "zhihu": _zhihu}


def _available() -> list[str]:
    """Platforms that can actually run in this environment right now."""
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
                "(title, content, author, metrics, url). Platforms: reddit (needs "
                "REDDIT_CLIENT_ID/SECRET/USERNAME/PASSWORD for the live OAuth API; "
                "anonymous JSON may 403), x (needs X_BEARER_TOKEN), zhihu (unsupported). "
                "Use it to find community opinions, first-hand experiences, or current "
                "discussion on a topic."
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
