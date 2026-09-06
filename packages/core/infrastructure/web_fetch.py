"""Real page fetch + HTML→text cleaning for the research batch-EVIDENCE path.

``web_search`` only ever returns ``{title, url, snippet}`` — the model has no real page
body to judge. This module adds the missing capability: concurrently fetch a **batch** of
URLs, SSRF-guard every hop, clean each page's HTML down to its readable core text, and
classify how usable that text is. It is deliberately free of cloud/DB concerns: callers
(``plugins/research/plugin.py``) persist the cleaned drafts and record provenance.

The two trusted contracts handed to callers:

- :func:`canonical_url` — the **E5 canonical form** used as the graph's source identity
  key (lowercase scheme/host, fragment dropped, ``:80``/``:443`` stripped, ``www`` kept,
  path/query otherwise untouched). Everything downstream keys on this string.
- :func:`fetch_clean_urls` — one batch → one ``list[dict]`` envelope per input URL:
  ``{url, canonical_url, status, error, title, http_status, final_url,
  content_status, full_char_len, char_len, text, truncated}``.

``status`` is the *transport* verdict (``ok`` | ``error``). On ``ok``, ``content_status``
is the *content* verdict: ``usable`` (cleaned text length ≥ :data:`MIN_FETCH_TEXT_CHARS`
and no interstitial wall), ``empty`` (below the floor), or ``interstitial`` (login /
captcha / JS-required wall — leniently detected, never trusted to contain a claim).
Both non-``usable`` verdicts fail downstream verification deterministically.

Security posture (the ``research_scrape fetch`` action behind it is agent-invocable with
arbitrary URLs, so SSRF is a hard line): only ``http``/``https`` schemes; every URL *and
every manual redirect hop* (max :data:`MAX_REDIRECTS`) has its host resolved and **every**
returned address checked — loopback, RFC1918, link-local/CGNAT/ULA, IPv4-mapped IPv6 and
``0.0.0.0``/multicast/reserved are all refused; response bodies are read through a
streaming cap (:data:`DEFAULT_MAX_BODY_BYTES`) so an unbounded download is aborted.
Tests inject a fake ``resolver`` and an ``httpx.MockTransport`` so nothing is ever hit
offline.
"""
from __future__ import annotations

import asyncio
import inspect
import ipaddress
import re
import socket
from collections.abc import Callable
from urllib.parse import urljoin, urlparse

import httpx

try:
    from lxml import html as lxml_html
except ImportError:  # lxml is a declared dep; this is a safety net
    lxml_html = None


# ── trusted thresholds / tunables ─────────────────────────────────────────────
# Floor for calling cleaned page text "usable" (E2 verified gate: full_char_len >= MIN).
MIN_FETCH_TEXT_CHARS = 200
# Per-URL envelope text total budget the caller splits across a batch (E4: the whole tool
# message must stay under the 8k prompt cap, so each URL only ever sees a fair slice).
DEFAULT_TEXT_TARGET = 6000
# Full cleaned draft length persisted to the scrape store (E6 read-back ceiling).
DEFAULT_MAX_CHARS = 4000
DEFAULT_MAX_BODY_BYTES = 2 * 1024 * 1024  # abort a page whose body streams past 2 MiB
DEFAULT_TIMEOUT_S = 15.0
DEFAULT_MAX_CONCURRENCY = 6
MAX_REDIRECTS = 3

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Interstitial walls we must not mistake for content. Deliberately narrow + title-first:
# a real long article almost never titles itself "Log in", but short walls almost always do.
_INTERSTITIAL_TITLE_HINTS = (
    "log in", "sign in", "please enable javascript", "enable javascript and cookies",
    "verify you are human", "just a moment", "attention required", "access denied",
    "not available in your region", "you have been blocked", "captcha",
    # zh walls
    "安全验证", "人机验证", "访问验证", "验证码", "登录", "请开启javascript",
)
# Signal that only counts inside the FIRST bytes of cleaned text (a full-page wall, not a
# stray mention deep in an article).
_INTERSTITIAL_LEAD_HINTS = ("you have been blocked", "enable javascript and cookies")

# Private / reserved address space refused by the SSRF guard.
_PRIVATE_NETS = (
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),      # CGNAT
    ipaddress.ip_network("127.0.0.0/8"),        # loopback
    ipaddress.ip_network("169.254.0.0/16"),     # link-local (incl. cloud metadata)
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.0.0.0/24"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("198.18.0.0/15"),      # benchmarking
    ipaddress.ip_network("224.0.0.0/4"),        # multicast
    ipaddress.ip_network("240.0.0.0/4"),        # reserved / limited broadcast
    ipaddress.ip_network("::1/128"),            # IPv6 loopback
    ipaddress.ip_network("::/128"),             # unspecified
    ipaddress.ip_network("fc00::/7"),           # ULA
    ipaddress.ip_network("fe80::/10"),          # IPv6 link-local
)

_WS_RE = re.compile(r"\s+")


# ── canonical URL (E5) ────────────────────────────────────────────────────────
def canonical_url(raw: str) -> str:
    """Conservative canonical form of a URL: lowercase scheme/host, strip fragment and the
    default ``:80``/``:443`` port, drop userinfo. ``www`` is kept, path/query untouched.

    Anything not a parseable ``http(s)`` URL is returned lowercased/trimmed unchanged so a
    caller never crashes on junk — but such a value can never be a verified Source because
    the fetch layer will not have produced provenance for it either.
    """
    raw = (raw or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlparse(raw)
    except ValueError:
        return raw
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        return raw
    host = parsed.hostname
    if not host:
        return raw
    host = host.lower()
    port = None
    try:
        port = parsed.port
    except ValueError:  # malformed port — keep the raw authority untouched
        port = None
    # IPv6 literal hosts need their brackets back in the netloc.
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if port is None or port in (80, 443):
        netloc = host
    else:
        netloc = f"{host}:{port}"
    out = f"{scheme}://{netloc}{parsed.path or '/'}"
    if parsed.query:
        out = f"{out}?{parsed.query}"
    return out


# ── SSRF guard ────────────────────────────────────────────────────────────────
# ``resolver(host)`` returns every address the host currently resolves to; the default
# does a real ``getaddrinfo``. Tests inject a stub so the guard is exercised offline.
Resolver = Callable[[str], list[str]]


async def _default_resolver(host: str) -> list[str]:
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        return []
    out: list[str] = []
    for family, _, _, _, sockaddr in infos:
        addr = sockaddr[0] if isinstance(sockaddr, tuple) else sockaddr
        if addr and addr not in out:
            out.append(addr)
    return out


def _is_blocked(ip_str: str) -> bool:
    """True when an address string falls in a refused range."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # unparseable — refuse rather than fetch blind
    # IPv4-mapped IPv6 (``::ffff:127.0.0.1``) must be checked as its embedded IPv4 too.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return any(ip in net for net in _PRIVATE_NETS)


class SSRFBlockedError(ValueError):
    """A URL's host resolves to a private/reserved address — refused."""


async def _assert_public_host(url: str, resolver: Resolver) -> None:
    """Resolve ``url``'s host and refuse it unless every returned address is public."""
    try:
        parsed = urlparse(url)
    except ValueError as exc:
        raise SSRFBlockedError(f"unparseable URL: {url!r}") from exc
    scheme = (parsed.scheme or "").lower()
    host = parsed.hostname
    if scheme not in ("http", "https"):
        raise SSRFBlockedError(f"refusing non-http(s) URL scheme: {url!r}")
    if not host:
        raise SSRFBlockedError(f"refusing URL without a host: {url!r}")
    addresses = resolver(host)
    if inspect.isawaitable(addresses):
        addresses = await addresses
    if not addresses:
        raise SSRFBlockedError(f"could not resolve host: {host}")
    blocked = [a for a in addresses if _is_blocked(a)]
    if blocked:
        raise SSRFBlockedError(
            f"refusing URL whose host resolves to a private/reserved address: {host} -> {blocked[0]}"
        )


# ── response classification ───────────────────────────────────────────────────
class _BodyTooLarge(Exception):
    """Streamed response exceeded :data:`DEFAULT_MAX_BODY_BYTES` (abort the read)."""


def _classify_exc(exc: BaseException) -> tuple[str, str]:
    """Map a transport exception to ``(error_type, safe_message)``.

    Messages come from the exception *class* + HTTP code, never the exception text, so a
    URL/header detail that could embed a secret never reaches the tool result.
    """
    if isinstance(exc, SSRFBlockedError):
        return "ssrf_blocked", str(exc)
    if isinstance(exc, _BodyTooLarge):
        return "body_too_large", "response body exceeded the download cap"
    if isinstance(exc, httpx.TimeoutException):
        return "timeout", "request timed out"
    if isinstance(exc, httpx.ConnectError):
        return "network", "could not connect to the host"
    if isinstance(exc, httpx.NetworkError):
        return "network", "network error reaching the host"
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return "http_error", f"HTTP {code}"
    if isinstance(exc, httpx.TooManyRedirects):
        return "redirects", "too many redirects"
    if isinstance(exc, httpx.DecodingError):
        return "encoding", "could not decode the response body"
    name = type(exc).__name__
    return "unknown", f"fetch failed ({name})"


# ── charset + HTML → core text ────────────────────────────────────────────────
_META_CHARSET_RE = re.compile(
    r"""<meta[^>]+charset\s*=\s*["']?\s*([a-zA-Z0-9_\-]+)""", re.IGNORECASE
)
_META_CT_RE = re.compile(
    r"""<meta[^>]+http-equiv\s*=\s*["']?content-type["']?[^>]+content\s*=\s*["'][^"']*charset=([a-zA-Z0-9_\-]+)""",
    re.IGNORECASE,
)


def _decode_html(content: bytes) -> str:
    """Decode raw response bytes to text, honoring header/meta-declared charsets.

    A real ``utf-8`` first, then the zh-supertet ``gb18030`` for mislabelled GBK/GB2312
    sites, then ``latin-1`` (which never fails) as the final fallback. Decoded with
    ``errors="replace"`` so a bad byte never takes down the whole batch.
    """
    head = content[:8192].decode("latin-1", errors="replace")
    declared = ""
    m = re.search(r"charset\s*=\s*[\"']?([a-zA-Z0-9_\-]+)", head)
    if m:
        declared = m.group(1).lower()
    if not declared:
        # explicit ``<meta ... charset=...>``
        m = _META_CHARSET_RE.search(head)
        if m:
            declared = m.group(1).lower()
    if not declared:
        # ``<meta http-equiv="Content-Type" content="...; charset=...">``
        m = _META_CT_RE.search(head)
        if m:
            declared = m.group(1).lower()
    for enc in ([declared] if declared else []) + ["utf-8", "gb18030", "latin-1"]:
        try:
            return content.decode(enc, errors="replace")
        except LookupError:
            continue
    return content.decode("utf-8", errors="replace")


def _clean_html(html_text: str) -> tuple[str, str]:
    """Strip chrome/scripts from HTML → ``(title, readable_text)``.

    Removes ``script/style/nav/header/footer/aside/form/iframe/noscript/svg`` elements and
    HTML comments, then flattens to text and collapses whitespace. Title comes from
    ``<title>`` with an ``og:title`` fallback.
    """
    if lxml_html is None:
        return "", _WS_RE.sub(" ", html_text or "").strip()
    try:
        doc = lxml_html.fromstring(html_text)
    except Exception:  # noqa: BLE001 - unparseable page → treat as text, never fatal
        return "", _WS_RE.sub(" ", html_text or "").strip()
    for node in doc.xpath(
        "//script|//style|//nav|//header|//footer|//aside|//form|//iframe|//noscript|//svg"
    ):
        parent = node.getparent()
        if parent is not None:
            parent.remove(node)
    title = " ".join(t.strip() for t in (doc.xpath("//title//text()") or []))
    if not title:
        og = doc.xpath('//meta[@property="og:title"]/@content')
        title = (og[0] if og else "").strip()
    body = doc.text_content() or ""
    body = _WS_RE.sub(" ", body).strip()
    return title.strip(), body


def _content_status(title: str, text: str) -> str:
    """usable / empty / interstitial verdict for a successfully fetched page."""
    # Interstitial is checked BEFORE the length floor: a short login/captcha wall should be
    # reported as a wall (actionable: this source is not fetchable), not as an empty page.
    low_title = (title or "").lower()
    if any(h in low_title for h in _INTERSTITIAL_TITLE_HINTS):
        return "interstitial"
    lead = text[:160].lower()
    if any(h in lead for h in _INTERSTITIAL_LEAD_HINTS):
        return "interstitial"
    if len(text) < MIN_FETCH_TEXT_CHARS:
        return "empty"
    return "usable"


# ── per-URL fetch ─────────────────────────────────────────────────────────────
async def _read_bounded(resp: httpx.Response, max_body_bytes: int) -> bytes:
    """Drain a streaming response body, aborting past ``max_body_bytes``."""
    chunks: list[bytes] = []
    total = 0
    async for chunk in resp.aiter_bytes():
        total += len(chunk)
        if total > max_body_bytes:
            raise _BodyTooLarge()
        chunks.append(chunk)
    return b"".join(chunks)


async def _fetch_one(
    url: str,
    *,
    resolver: Resolver,
    headers: dict,
    client: httpx.AsyncClient,
    max_body_bytes: int,
    per_url_chars: int,
    max_chars: int,
) -> dict:
    """Fetch one URL with per-hop SSRF checks + manual redirects → result envelope."""
    current = url
    try:
        await _assert_public_host(current, resolver)
        for _ in range(MAX_REDIRECTS + 1):
            req = client.build_request("GET", current, headers=headers)
            resp = await client.send(req, stream=True)
            try:
                if resp.status_code in (301, 302, 303, 307, 308):
                    location = resp.headers.get("location")
                    if not location:
                        break
                    next_url = urljoin(current, location)
                    # A redirect may hop to a private/metadata target — re-guard each hop.
                    await _assert_public_host(next_url, resolver)
                    current = next_url
                    continue
                if resp.status_code >= 400:
                    error_type, message = _classify_exc(
                        httpx.HTTPStatusError(
                            f"HTTP {resp.status_code}", request=req, response=resp
                        )
                    )
                    return {
                        "url": url,
                        "canonical_url": canonical_url(url),
                        "status": "error",
                        "error": {"type": error_type, "message": message},
                        "title": "",
                        "http_status": resp.status_code,
                        "final_url": canonical_url(current),
                        "content_status": "n/a",
                        "full_char_len": 0,
                        "char_len": 0,
                        "text": "",
                        "truncated": False,
                    }
                content = await _read_bounded(resp, max_body_bytes)
                break
            finally:
                await resp.aclose()
        else:
            return {
                "url": url,
                "canonical_url": canonical_url(url),
                "status": "error",
                "error": {"type": "redirects", "message": "too many redirects"},
                "title": "",
                "http_status": None,
                "final_url": canonical_url(current),
                "content_status": "n/a",
                "full_char_len": 0,
                "char_len": 0,
                "text": "",
                "truncated": False,
            }
    except Exception as exc:  # noqa: BLE001 - any transport fault → an honest error envelope
        error_type, message = _classify_exc(exc)
        return {
            "url": url,
            "canonical_url": canonical_url(url),
            "status": "error",
            "error": {"type": error_type, "message": message},
            "title": "",
            "http_status": None,
            "final_url": canonical_url(current),
            "content_status": "n/a",
            "full_char_len": 0,
            "char_len": 0,
            "text": "",
            "truncated": False,
        }
    # A successful body: decode + clean, then classify usability.
    html_text = _decode_html(content)
    title, full = _clean_html(html_text)
    if len(full) > max_chars:
        full = full[:max_chars]
    full_char_len = len(full)
    content_status = _content_status(title, full)
    shown = full[:per_url_chars]
    return {
        "url": url,
        "canonical_url": canonical_url(url),
        "status": "ok",
        "title": title[:500],
        "http_status": resp.status_code,
        "final_url": canonical_url(current),
        "content_status": content_status,
        "full_char_len": full_char_len,
        "char_len": len(shown),
        "text": shown,
        # Full cleaned draft (≤ max_chars) — the caller persists this to its scrape store.
        # It is deliberately NOT part of what the model sees (the tool message budget (E4)
        # only gets the ``text`` slice); consumers strip ``body`` before returning a result.
        "body": full,
        "truncated": full_char_len > len(shown),
    }


async def fetch_clean_urls(
    urls: list[str],
    *,
    text_target: int = DEFAULT_TEXT_TARGET,
    max_chars: int = DEFAULT_MAX_CHARS,
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
    user_agent: str = _USER_AGENT,
    transport: httpx.AsyncBaseTransport | None = None,
    resolver: Resolver | None = None,
) -> list[dict]:
    """Fetch + clean a batch of URLs concurrently; one envelope per input URL.

    The order mirrors ``urls`` (independent of completion order) so a caller's index/claim
    pairing stays stable. A single page failing never fails the batch — it returns an
    ``error`` envelope and the healthy pages carry on. Each returned ``text`` is capped at
    an even slice of ``text_target`` (E4), so the model-facing part of the batch serializes
    within the prompt budget; each envelope also carries ``body`` (the full cleaned draft,
    ≤ ``max_chars``) that a caller persists to its scrape store but must strip before it
    returns the envelope to the model.
    """
    res = resolver or _default_resolver
    dedup: list[str] = []
    for u in urls:
        u = (u or "").strip()
        if u and u not in dedup:
            dedup.append(u)
    if not dedup:
        return []
    n = len(dedup)
    per_url_chars = max(1, int(text_target) // n)
    headers = {
        "User-Agent": user_agent or _USER_AGENT,
        "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    }
    sem = asyncio.Semaphore(max(1, int(max_concurrency)))
    results: list[dict | None] = [None] * n
    timeout = httpx.Timeout(timeout_s)

    async def _guarded(i: int, u: str) -> None:
        # Per-call client so one request's connection state never bleeds into another.
        async with sem, httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        ) as client:
            try:
                results[i] = await _fetch_one(
                    u,
                    resolver=res,
                    headers=headers,
                    client=client,
                    max_body_bytes=max_body_bytes,
                    per_url_chars=per_url_chars,
                    max_chars=max(1, int(max_chars)),
                )
            except Exception as exc:  # noqa: BLE001 - never let one URL kill the batch
                error_type, message = _classify_exc(exc)
                results[i] = {
                    "url": u,
                    "canonical_url": canonical_url(u),
                    "status": "error",
                    "error": {"type": error_type, "message": message},
                    "title": "",
                    "http_status": None,
                    "final_url": canonical_url(u),
                    "content_status": "n/a",
                    "full_char_len": 0,
                    "char_len": 0,
                    "text": "",
                    "truncated": False,
                }

    await asyncio.gather(*(_guarded(i, u) for i, u in enumerate(dedup)))
    return [r for r in results if r is not None]
