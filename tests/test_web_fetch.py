"""Unit tests for the batch-EVIDENCE fetch layer (core.infrastructure.web_fetch).

Covers the two trusted contracts a caller relies on:
- ``canonical_url`` — the E5 conservative canonical form (identity key for graph Sources).
- ``fetch_clean_urls`` — SSRF-guarded concurrent fetch + HTML→core-text cleaning with a
  usable/empty/interstitial content verdict, and the E4 per-URL text budget.

Everything runs against an ``httpx.MockTransport`` + a stub resolver so no real network (or
DNS) is ever touched in CI.
"""
from __future__ import annotations

import json
from urllib.parse import urlparse

import httpx
import pytest
from core.infrastructure.web_fetch import (
    MIN_FETCH_TEXT_CHARS,
    _decode_html,
    _is_blocked,
    canonical_url,
    fetch_clean_urls,
)

_PUBLIC = "93.184.216.34"  # whatever public address — the stub always hands this back


def _public_resolver(host: str) -> list[str]:
    return [_PUBLIC]


def _long_body(word: str = "tomato", n: int = 40) -> str:
    """A body comfortably above the MIN_FETCH_TEXT_CHARS usable floor."""
    return " ".join([word] * n)


# ── E5 canonical_url ─────────────────────────────────────────────────────────
def test_canonical_lowercases_scheme_host_strips_fragment_and_default_port():
    assert (
        canonical_url("HTTP://Example.COM:80/a?q=1#frag")
        == "http://example.com/a?q=1"
    )
    assert canonical_url("https://WWW.example.com:443/x") == "https://www.example.com/x"
    assert canonical_url("http://example.com:8080/y") == "http://example.com:8080/y"
    assert canonical_url("http://[::1]:8080/x") == "http://[::1]:8080/x"


def test_canonical_drops_userinfo():
    assert canonical_url("https://user:pass@Example.COM:80/z") == "https://example.com/z"


def test_canonical_www_differs_from_apex():
    # E5 deliberately KEEPS www — the two are distinct canonical identities.
    assert canonical_url("https://www.example.com/x") != canonical_url("https://example.com/x")


def test_canonical_non_http_url_returns_raw():
    assert canonical_url("not-a-url") == "not-a-url"
    assert canonical_url("javascript:alert(1)") == "javascript:alert(1)"


def test_canonical_empty():
    assert canonical_url("  ") == ""


# ── SSRF guard ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "addr",
    [
        "127.0.0.1",
        "127.0.0.2",
        "10.0.0.1",
        "172.16.0.1",
        "172.31.255.1",
        "192.168.1.1",
        "169.254.169.254",  # cloud metadata
        "100.64.0.1",  # CGNAT
        "0.0.0.0",
        "224.0.0.1",
        "::1",
        "::ffff:127.0.0.1",  # IPv4-mapped loopback
        "fc00::1",
        "fe80::1",
    ],
)
def test_is_blocked_rejects_private_and_reserved(addr):
    assert _is_blocked(addr) is True


@pytest.mark.parametrize("addr", ["93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"])
def test_is_blocked_allows_public(addr):
    assert _is_blocked(addr) is False


async def test_fetch_refuses_private_host_before_transport():
    # The guard fires on resolution — the (200-returning) transport is never reached.
    transport = httpx.MockTransport(lambda req: httpx.Response(200, text="<p>hi</p>"))
    out = await fetch_clean_urls(
        ["https://169.254.169.254/latest/meta-data/"],
        transport=transport,
        resolver=lambda h: ["169.254.169.254"],
    )
    assert out[0]["status"] == "error"
    assert out[0]["error"]["type"] == "ssrf_blocked"


async def test_fetch_refuses_redirect_hop_to_private():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "evil.example":
            return httpx.Response(302, headers={"location": "http://169.254.169.254/x"})
        return httpx.Response(200, text="<p>never reached</p>")

    def resolver(host: str) -> list[str]:
        return ["127.0.0.1"] if "169.254.169.254" in host else [_PUBLIC]

    out = await fetch_clean_urls(
        ["https://evil.example/start"], transport=httpx.MockTransport(handler), resolver=resolver
    )
    assert out[0]["status"] == "error"
    assert out[0]["error"]["type"] == "ssrf_blocked"


async def test_fetch_refuses_non_http_scheme():
    # The guard only ever allows http(s) — a non-http scheme becomes an honest error
    # envelope (never a transport attempt).
    transport = httpx.MockTransport(lambda req: httpx.Response(200))
    out = await fetch_clean_urls(
        ["ftp://example.com/file"], transport=transport, resolver=_public_resolver
    )
    assert out[0]["status"] == "error"
    assert out[0]["error"]["type"] == "ssrf_blocked"


# ── happy-path fetch + content verdicts ──────────────────────────────────────
def _page_handler(request: httpx.Request) -> httpx.Response:
    host = request.url.host
    if host == "example.com":
        return httpx.Response(
            200,
            text=(
                "<html><head><title>Tomato Cooking Guide</title></head><body>"
                "<script>bad()</script><nav>chrome</nav><article>"
                f"{_long_body('heat', 300)}"
                "</article></body></html>"
            ),
        )
    if host == "empty.example":
        return httpx.Response(200, text="<html><body><p>tiny</p></body></html>")
    if host == "login.example":
        return httpx.Response(
            200, text="<html><title>Sign in</title><body><p>please log in to read</p></body></html>"
        )
    if host == "redirect.example":
        return httpx.Response(302, headers={"location": "https://example.com/final"})
    return httpx.Response(404, text="nope")


async def test_fetch_clean_urls_batch_envelopes_and_redirects():
    transport = httpx.MockTransport(_page_handler)
    out = await fetch_clean_urls(
        [
            "https://example.com/recipe",
            "https://empty.example/x",
            "https://login.example/y",
            "https://redirect.example/z",
            "https://missing.example/nope",
        ],
        transport=transport,
        resolver=_public_resolver,
    )
    by_host = {urlparse(o["url"]).hostname: o for o in out}
    assert len(by_host) == 5

    ok = by_host["example.com"]
    assert ok["status"] == "ok"
    assert ok["content_status"] == "usable"
    assert ok["full_char_len"] >= MIN_FETCH_TEXT_CHARS
    assert ok["title"] == "Tomato Cooking Guide"
    assert "chrome" not in ok["text"]  # nav stripped
    assert ok["canonical_url"] == "https://example.com/recipe"

    empty = by_host["empty.example"]
    assert empty["status"] == "ok"
    assert empty["content_status"] == "empty"
    assert empty["full_char_len"] < MIN_FETCH_TEXT_CHARS

    login = by_host["login.example"]
    assert login["status"] == "ok"
    assert login["content_status"] == "interstitial"

    redir = by_host["redirect.example"]
    assert redir["status"] == "ok"
    assert redir["final_url"] == "https://example.com/final"

    missing = by_host["missing.example"]
    assert missing["status"] == "error"
    assert missing["error"]["type"] == "http_error"


async def test_fetch_text_slice_honors_even_text_target_budget():
    # 3 usable URLs → each model-facing text slice ≤ text_target // 3 = 2000 chars.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=f"<html><body><p>{_long_body('words', 2000)}</p></body></html>",
        )

    transport = httpx.MockTransport(handler)
    out = await fetch_clean_urls(
        ["https://example.com/1", "https://example.com/2", "https://example.com/3"],
        transport=transport,
        resolver=_public_resolver,
        text_target=6000,
    )
    for o in out:
        assert o["char_len"] <= 2000
        assert len(o["text"]) == o["char_len"]
    # The consumer strips ``body`` before handing the batch to the model: that stripped view
    # must serialize inside the 8k prompt cap (E4 hard assertion at the tool boundary).
    views = [{k: v for k, v in o.items() if k != "body"} for o in out]
    assert len(json.dumps(views, ensure_ascii=False)) <= 7200


async def test_fetch_charset_from_meta_declaration():
    # A GB2312 page (no HTTP charset header) must decode via its <meta> declaration.
    body_bytes = (
        "<html><head><meta charset='gb2312'></head><body>"
        "<p>西红柿炒鸡蛋方法如下。</p><p>" + ("炒。" * 200) + "</p></body></html>"
    ).encode("gb2312")
    transport = httpx.MockTransport(
        lambda req: httpx.Response(
            200, content=body_bytes, headers={"content-type": "text/html"}
        )
    )
    out = await fetch_clean_urls(
        ["https://zh.example.com/recipe"], transport=transport, resolver=_public_resolver
    )
    assert out[0]["status"] == "ok"
    assert "西红柿炒鸡蛋" in out[0]["text"]


def test_decode_html_falls_back_without_crashing():
    # latin-1 is the never-fail final fallback; junk bytes must not raise.
    decoded = _decode_html(b"\xff\xfe\x00 garbage \x80")
    assert "garbage" in decoded


async def test_batch_error_does_not_kill_healthy_pages():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "good.example":
            return httpx.Response(200, text=f"<p>{_long_body('fine')}</p>")
        raise httpx.ConnectError("boom")

    out = await fetch_clean_urls(
        ["https://good.example/a", "https://bad.example/b"],
        transport=httpx.MockTransport(handler),
        resolver=_public_resolver,
    )
    statuses = [o["status"] for o in out]
    assert statuses == ["ok", "error"]
    assert out[1]["error"]["type"] == "network"
