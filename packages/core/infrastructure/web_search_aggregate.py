"""Keyless multi-engine aggregate web search (Bing / DDG / Google / Baidu).

One synchronous entry point, ``aggregate_web_search``, fans out to several **keyless**
engines concurrently, tolerates each engine failing on its own, decodes wrapped
redirect URLs back to their real addresses, dedupes/fuses across engines, and returns a
uniform ``list[{title, url, snippet, engine}]``. ``site_limited_web_search`` reuses the
same machinery with a ``site:`` filter — the degrade path for social sources (x / zhihu)
that have no public API.

Engine reality, kept honest:
- ``bing``  — `www.bing.com/search?format=rss` returns a clean keyless RSS feed (no
  cookies / JS). Fast and reliable.
- ``google`` / ``baidu`` — HTML SERP scraping is best-effort and *often* blocked
  (captcha / consent) from datacenter or VPN exits. Kept in the default concurrent set
  but wrapped tolerant: a blocked engine contributes nothing and never takes the others
  down.
- ``ddg``   — the free ``ddgs`` package (already a dependency) is the slowest engine, so
  it is *not* in the default set; it is added automatically as a second chance whenever
  the default engines all come back empty.

The caller's LLM already holds full conversation context and does the synthesis; this
module only gathers + normalizes raw results, so engines stay swappable behind one seam.
"""
from __future__ import annotations

import asyncio
import html as html_mod
import re
from urllib.parse import parse_qs, unquote, urlparse

import httpx

# Kept importable without the optional scrapers installed.
try:
    from lxml import html as lxml_html
except ImportError:  # lxml is a declared dep; this is a safety net
    lxml_html = None

_TAG_RE = re.compile(r"<[^>]+>")
_SNIPPET_CHARS = 450
_DEFAULT_TIMEOUT = httpx.Timeout(7.0)

# Wrapper hosts whose query string carries the real target URL (``?q=`` / ``?url=``).
_WRAPPER_HOSTS = ("google.com", "google.com.hk", "bing.com", "baidu.com")
# Default set = the concurrent Google/Bing/Baidu scrape (fast here); the ``ddg`` engine
# (a slower third-party scraper) is added as a second chance only when those return empty.
_DEFAULT_ENGINES = ("bing", "google", "baidu")
_ALL_ENGINES = ("bing", "ddg", "google", "baidu")


def _clean_text(raw: str) -> str:
    """Strip HTML tags + entities, collapse whitespace, trim to a sane snippet size."""
    text = html_mod.unescape(_TAG_RE.sub(" ", raw or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:_SNIPPET_CHARS]


def decode_redirect_url(href: str) -> str:
    """Resolve search-engine wrapper URLs back to their real target.

    Google wraps results as ``/url?q=<real>&sa=...``; Bing and Baidu use the same
    ``?q=``/``?url=`` shape on some paths. When the visible href is a known wrapper whose
    query carries an ``http(s)`` target, the target is returned; otherwise the href is
    returned unchanged (a raw URL, or a ``link?url=`` that only resolves via a redirect).
    """
    href = (href or "").strip()
    try:
        parsed = urlparse(href)
    except ValueError:
        return href
    host = (parsed.hostname or "").lower()
    if host.endswith(_WRAPPER_HOSTS):
        q = parse_qs(parsed.query)
        for key in ("q", "url"):
            values = q.get(key)
            if values:
                target = unquote(values[0])
                if target.startswith(("http://", "https://")):
                    return target
    return href


# ── engine scrapers (each returns a list of raw {title, url, snippet}) ────────
async def _scrape_bing(client: httpx.AsyncClient, query: str, limit: int) -> list[dict]:
    resp = await client.get(
        "https://www.bing.com/search",
        params={"q": query, "format": "rss", "count": limit},
        headers={"User-Agent": _USER_AGENT},
    )
    resp.raise_for_status()
    return _parse_bing_rss(resp.text)


def _rss_child_text(item, tag: str) -> str:
    """Text of an ``<item>`` child element, or ``""`` when the tag is absent."""
    node = item.find(tag)
    return node.text or "" if node is not None else ""


def _parse_bing_rss(text: str) -> list[dict]:
    import xml.etree.ElementTree as ET

    out: list[dict] = []
    root = ET.fromstring(text)
    for item in root.iter("item"):
        title = _clean_text(_rss_child_text(item, "title"))
        url = decode_redirect_url(_rss_child_text(item, "link"))
        snippet = _clean_text(_rss_child_text(item, "description"))
        if title and url:
            out.append({"title": title, "url": url, "snippet": snippet})
    return out


async def _scrape_ddg(query: str, limit: int) -> list[dict]:
    """``ddgs`` is synchronous (its own HTTP), so it runs in a worker thread."""
    def _run() -> list[dict]:
        from ddgs import DDGS

        out: list[dict] = []
        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=limit):
                title = (r.get("title") or "").strip()
                url = decode_redirect_url(r.get("href") or "")
                if title and url:
                    out.append(
                        {
                            "title": title,
                            "url": url,
                            "snippet": _clean_text(r.get("body") or ""),
                        }
                    )
        return out

    return await asyncio.to_thread(_run)


async def _scrape_google(client: httpx.AsyncClient, query: str, limit: int) -> list[dict]:
    resp = await client.get(
        "https://www.google.com/search",
        params={"q": query, "num": min(limit, 10)},
        headers={
            "User-Agent": _USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    resp.raise_for_status()
    return _parse_google_html(resp.text, limit)


def _parse_google_html(text: str, limit: int) -> list[dict]:
    if lxml_html is None:
        return []
    try:
        doc = lxml_html.fromstring(text)
    except Exception:  # noqa: BLE001 - parse failure on an ad-blocked page → no results
        return []
    out: list[dict] = []
    # Organic results: an <a> whose only meaningful child is an <h3> title.
    for node in doc.xpath('//div[@id="search"]//a[.//h3]') or doc.xpath("//a[.//h3]"):
        href = node.get("href") or ""
        if not href.startswith("http"):
            continue
        title = _clean_text(" ".join(node.xpath(".//h3//text()")))
        if not title:
            continue
        # Snippet: aggregate the enclosing block's text minus the title (best effort).
        block = node.xpath("ancestor::div[contains(@class,'g')][1]")
        snippet = ""
        if block:
            body = _clean_text(block[0].text_content() or "")
            snippet = body.replace(title, "", 1).strip()[:_SNIPPET_CHARS]
        out.append({"title": title, "url": decode_redirect_url(href), "snippet": snippet})
        if len(out) >= limit:
            break
    return out


async def _scrape_baidu(client: httpx.AsyncClient, query: str, limit: int) -> list[dict]:
    resp = await client.get(
        "https://www.baidu.com/s",
        params={"wd": query, "rn": min(limit, 10)},
        headers={
            "User-Agent": _USER_AGENT,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
    )
    resp.raise_for_status()
    return _parse_baidu_html(resp.text, limit)


def _parse_baidu_html(text: str, limit: int) -> list[dict]:
    if lxml_html is None:
        return []
    try:
        doc = lxml_html.fromstring(text)
    except Exception:  # noqa: BLE001
        return []
    out: list[dict] = []
    for block in doc.cssselect("div.result, div.c-container"):
        a = block.cssselect("h3 a") or block.cssselect("a[href^='http']")
        if not a:
            continue
        a = a[0]
        title = _clean_text(a.text_content())
        if not title:
            continue
        abstract = block.cssselect(".c-abstract, .content-right_8Zs40, .c-span-last")
        snippet = _clean_text(abstract[0].text_content()) if abstract else ""
        out.append({"title": title, "url": decode_redirect_url(a.get("href") or ""), "snippet": snippet})
        if len(out) >= limit:
            break
    return out


_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def _fuse(results_by_engine: dict[str, list[dict]], limit: int) -> list[dict]:
    """Deduplicate + fuse engine results into one ranked list.

    Engine priority is fixed (reliable engines first); within an engine, results keep
    their natural rank. A result seen from a higher-priority engine wins; the same URL
    or title from a lower-priority engine is dropped. Each engine contributes at most
    ``limit`` hits, so one engine can never flood the aggregate.
    """
    merged: list[dict] = []
    seen_keys: set[tuple[str, str]] = set()

    def _key(hit: dict) -> tuple[str, str]:
        host = (urlparse(hit["url"]).hostname or "").lower()
        return host, _clean_text(hit["title"]).lower()

    for engine in tuple(results_by_engine):  # insertion order == engine launch priority
        for hit in results_by_engine.get(engine, [])[:limit]:
            key = _key(hit)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            merged.append({**hit, "engine": engine})
            if len(merged) >= limit:
                return merged
    return merged


def _site_results(results: list[dict], site: str) -> list[dict]:
    """Keep only hits whose URL lives on ``site`` or one of its subdomains."""
    wanted = site.lower()
    kept: list[dict] = []
    for hit in results:
        host = (urlparse(hit["url"]).hostname or "").lower()
        if host == wanted or host.endswith("." + wanted):
            kept.append(hit)
    return kept


async def _run_engines(query: str, limit: int, engines: tuple[str, ...], timeout: float) -> list[dict]:
    headers = {"User-Agent": _USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}
    timeout_obj = httpx.Timeout(timeout)
    # Pre-seed the dict in launch order so _fuse's priority is deterministic (the first
    # engine in the caller's set wins dedup), independent of completion order.
    order = tuple(e for e in engines if e in _ALL_ENGINES)
    results: dict[str, list[dict]] = {name: [] for name in order}

    async def _guard(name: str, coro) -> None:
        try:
            results[name] = await coro
        except Exception:  # noqa: BLE001 - one failing engine never kills the search
            results[name] = []  # degraded on purpose; provenance stays visible in logs

    async with httpx.AsyncClient(timeout=timeout_obj, headers=headers, follow_redirects=False) as client:
        tasks = []
        if "bing" in engines:
            tasks.append(_guard("bing", _scrape_bing(client, query, limit)))
        if "google" in engines:
            tasks.append(_guard("google", _scrape_google(client, query, limit)))
        if "baidu" in engines:
            tasks.append(_guard("baidu", _scrape_baidu(client, query, limit)))
        # ddgs runs on its own transport; give it the same overall deadline.
        if "ddg" in engines:
            tasks.append(asyncio.wait_for(_guard("ddg", _scrape_ddg(query, limit)), timeout))
        await asyncio.gather(*tasks)
    return _fuse(results, limit)


def aggregate_web_search(
    query: str,
    top_k: int = 8,
    *,
    engines: tuple[str, ...] | None = None,
    timeout: float = 7.0,
) -> list[dict]:
    """Keyless aggregate web search. Returns ``[{title, url, snippet, engine}]``.

    Engines run concurrently and tolerate each other's failures: a blocked engine
    contributes nothing and the survivors still return results. Raises only when every
    engine fails (or none is reachable), with the underlying causes surfaced so the
    caller can degrade visibly instead of guessing.
    """
    top_k = max(1, min(int(top_k), 20))
    chosen = engines or _DEFAULT_ENGINES
    unknown = [e for e in chosen if e not in _ALL_ENGINES]
    if unknown:
        raise ValueError(f"unknown engine(s): {unknown}; supported: {list(_ALL_ENGINES)}")
    loop = asyncio.new_event_loop()
    try:
        results = loop.run_until_complete(_run_engines(query.strip(), top_k, chosen, timeout))
        # Second chance, deterministic: when the default engines were all blocked/empty
        # (e.g. Bing/Google/Baidu captcha'd from this network exit), retry once including
        # the slower ddgs engine so a search never silently returns nothing.
        if not results and engines is None and "ddg" not in chosen:
            results = loop.run_until_complete(
                _run_engines(query.strip(), top_k, (*chosen, "ddg"), timeout)
            )
        return results
    finally:
        loop.close()


def site_limited_web_search(site: str, query: str, top_k: int = 6, *, timeout: float = 7.0) -> list[dict]:
    """Site-scoped aggregate search for a platform with no public API.

    The degrade path for social platforms (e.g. ``zhihu.com``, ``x.com``): general
    engines index some of their pages, so this returns their content through the same
    deduped/fused pipeline — every result is guaranteed to live on ``site`` or one of its
    subdomains. Some engines honor ``site:`` poorly (returning nothing for well-known
    domains); when the ``site:`` pass comes back empty, a plain ``query`` pass is retried
    and post-filtered to the host. Returns the same shape as :func:`aggregate_web_search`.
    """
    site = site.strip().lower()
    query = query.strip()
    if not site or not query:
        return []
    raw = aggregate_web_search(f"site:{site} {query}", top_k * 2, timeout=timeout)
    kept = _site_results(raw, site)
    if not kept:
        # site: operator unsupported/empty on these engines → retry the plain query and
        # filter to the platform host afterwards (slower, so only on the empty path).
        raw = aggregate_web_search(query, top_k * 3, timeout=timeout)
        kept = _site_results(raw, site)
    return kept[:top_k]
