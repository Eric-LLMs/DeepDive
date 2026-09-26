"""``web_search``: search the web for up-to-date information via the provider seam."""
from __future__ import annotations

import asyncio
import json

from agent import Context, ToolExecution, ToolOutput, ToolRuntime, define_tool, text_block
from agent.tools.tool_permissions import ToolPermission


def _coerce_top_k(raw) -> int:
    """Lenient top_k: qwen-class models frequently send integers as ``\"5\"`` / ``5.0``.

    Strict Draft7 rejection of those was raising ToolArgsError before the provider was
    ever called (~0.2 ms fail), and the model then re-sent the same malformed value
    step after step (2026-09-26 incident: 4 fast failures, 30 s turn). Anything
    un-coercible falls back to the default rather than failing the turn.
    """
    try:
        value = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        value = 5
    return max(1, min(value, 20))


# Source extension (2026-09-26 operator ruling): beyond the keyed search API, the
# tool ALSO scrapes keyless SERPs (Bing URL-concat, like research does) and crawls
# page text for the top hits. Everything ships as ONE appended list to the model —
# [api results..., serp results..., page texts...] — deduped by URL, so a single
# web_search call delivers breadth (multiple engines) and depth (real page content).
_SERP_ENGINES = ("bing", "google", "baidu")
_PAGE_TOP_N = 3
_PAGE_TEXT_CHARS = 1500


async def _scrape_serps(query: str, top_k: int) -> list[dict]:
    """Keyless engine scrape (Bing RSS + Google/Baidu HTML), best-effort.

    Reuses the aggregate module — same code the ``aggregate`` provider runs. A total
    engine blackout or captcha returns []; it must never fail the API-backed search.
    """
    try:
        from core.infrastructure.web_search_aggregate import aggregate_web_search

        return await asyncio.to_thread(
            aggregate_web_search, query, top_k, engines=_SERP_ENGINES
        ) or []
    except Exception:  # noqa: BLE001 - extra source, not the primary path
        return []


async def _crawl_pages(results: list[dict]) -> list[dict]:
    """Crawl the top hits' full text through the research fetcher, as page entries."""
    urls = [r.get("url") for r in results[:_PAGE_TOP_N] if r.get("url")]
    if not urls:
        return []
    try:
        from core.infrastructure.web_fetch import fetch_clean_urls

        envelopes = await fetch_clean_urls(urls, text_target=_PAGE_TEXT_CHARS * len(urls))
    except Exception:  # noqa: BLE001 - depth is optional; snippets already answer
        return []
    pages: list[dict] = []
    for e in envelopes:
        if e.get("status") == "ok":
            text = (e.get("text") or "")[:_PAGE_TEXT_CHARS]
            if text:
                pages.append({
                    "type": "page",
                    "url": e.get("url", ""),
                    "title": e.get("title", ""),
                    "text": text,
                })
    return pages


async def _provider_search_with_retry(provider, query: str, top_k: int) -> dict:
    """Give-up rule (2026-09-26): at most TWO provider attempts, short backoff between.

    A second failure is terminal for this call — the error text tells the model to
    stop retrying instead of burning a full ReAct step per attempt. Tavily's own DDG
    fallback rides inside attempt #1's envelope.
    """
    outcome = None
    for attempt in (1, 2):
        outcome = await asyncio.to_thread(provider.search, query, top_k)
        if outcome.get("status") != "degraded":
            break
        if attempt == 1:
            await asyncio.sleep(0.8)
    return outcome


def register(runtime: ToolRuntime, ctx: Context, llm) -> None:
    async def web_search(args: dict, exec: ToolExecution) -> list[dict]:
        provider = ctx.resolve("web_search")
        if provider is None:
            raise RuntimeError("web search is not configured")
        query = args["query"]
        top_k = _coerce_top_k(args.get("top_k"))
        # The API search and the keyless SERP scrape are independent (SERP only needs
        # the query), so they run CONCURRENTLY — each inside its own worker thread —
        # and the turn pays max(api, serp), not the sum. Page crawling stays after
        # the merge because it consumes the merged top hits; it is itself concurrent
        # across URLs inside fetch_clean_urls.
        outcome, serp = await asyncio.gather(
            _provider_search_with_retry(provider, query, top_k),
            _scrape_serps(query, top_k),
        )
        if outcome.get("status") == "degraded":
            # A real engine outage must surface as a tool failure — never masquerade as a
            # normal "0 results" search, so gate diagnostics can tell the two apart.
            err = outcome.get("error") or {}
            raise RuntimeError(
                f"web search unavailable after 2 attempts (provider={outcome.get('provider')}, "
                f"{err.get('type', 'error')}): {err.get('message', 'no details')} — "
                "give up on web_search for this turn: answer without it (and say the web "
                "was unreachable); do NOT retry this tool."
            )
        results = outcome.get("results") or []
        # Append keyless SERP hits (dedupe by URL against the API results).
        seen = {r.get("url") for r in results if r.get("url")}
        for r in serp:
            u = r.get("url")
            if u and u not in seen:
                results.append(r)
                seen.add(u)
        # Append crawled page text for the top hits.
        pages = await _crawl_pages(results)
        return results + pages

    runtime.register(
        define_tool(
            name="web_search",
            description="Search the web for up-to-date information. Returns a list of "
            "results with title, url, snippet, and (for the top hits) page_text with "
            "the fetched page content. Use this when the answer needs external or "
            "recent knowledge beyond the local learning material.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The search query."},
                    # Lenient type set + in-body coercion: strict "integer" rejected the
                    # model's ``"5"``/``5.0`` and turned each rejection into a full
                    # wasted ReAct step (see _coerce_top_k).
                    "top_k": {
                        "type": ["integer", "string", "number"],
                        "description": "Number of results.",
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
            execute=web_search,
            # READ security class (operator ruling 2026-09-26): read-only web search is a
            # Chat capability, not a human decision — it must never raise a sandbox approval
            # popup, so it sits in the default-allowed READ class alongside rag_search.
            # CONSEQUENCE, accepted knowingly: the private_only / private_first source-policy
            # HARD DENY fences by the NETWORK class (sandbox._turn_denied), so a "answer only
            # from my knowledge base" turn no longer blocks this tool. The fence mechanism
            # itself is unchanged and still guards every other NETWORK-class tool
            # (search_social, bash); only web_search opted out, by product decision.
            permission={ToolPermission.READ},
            # P3-5: pure network I/O against the provider seam, no local state — the
            # frozen loop serializes tools without this flag (Run 9: 13 calls, Σ 28.3 s);
            # marking it safe lets same-step search calls fan out.
            is_concurrency_safe=True,
        )
    )
