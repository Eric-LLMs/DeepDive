"""Deterministic (offline) tests for the keyless aggregate web-search helpers."""
from core.infrastructure.web_search_aggregate import (
    _clean_text,
    _fuse,
    _parse_bing_rss,
    _site_results,
    decode_redirect_url,
)


def test_decode_redirect_url_google_wrapper():
    wrapped = (
        "https://www.google.com/url?q=https%3A%2F%2Fexample.com%2Fpage"
        "&sa=U&ved=2ahUKEw"
    )
    assert decode_redirect_url(wrapped) == "https://example.com/page"


def test_decode_redirect_url_passthrough():
    direct = "https://example.com/page?a=1"
    assert decode_redirect_url(direct) == direct
    # Baidu link?url= is an opaque token resolved only by a redirect round-trip;
    # it must be returned unchanged, never guessed at.
    opaque = "https://www.baidu.com/link?url=abc-def-123"
    assert decode_redirect_url(opaque) == opaque


def test_clean_text_strips_tags_and_entities():
    assert _clean_text("<b>Hello</b> &amp; <i>world</i>") == "Hello & world"
    assert _clean_text("  lots   of\nwhitespace ") == "lots of whitespace"


def test_parse_bing_rss_sample():
    xml = (
        '<?xml version="1.0"?><rss><channel>'
        "<item><title>Rust Programming Language</title>"
        "<link>https://www.rust-lang.org/</link>"
        "<description>&lt;p&gt;A language empowering everyone.&lt;/p&gt;</description>"
        "</item>"
        "</channel></rss>"
    )
    out = _parse_bing_rss(xml)
    assert len(out) == 1
    assert out[0]["title"] == "Rust Programming Language"
    assert out[0]["url"] == "https://www.rust-lang.org/"
    assert "A language empowering everyone." in out[0]["snippet"]


def test_fuse_dedupes_across_engines():
    bing = [{"title": "Rust Lang", "url": "https://rust-lang.org/", "snippet": ""}]
    ddg = [
        {"title": "Rust Lang", "url": "https://rust-lang.org/", "snippet": ""},
        {"title": "Install Rust", "url": "https://rust-lang.org/tools/install", "snippet": ""},
    ]
    fused = _fuse({"bing": bing, "ddg": ddg}, limit=10)
    # Higher-priority engine's copy wins; the duplicate is dropped, the unique kept.
    assert [f["url"] for f in fused] == [
        "https://rust-lang.org/",
        "https://rust-lang.org/tools/install",
    ]
    assert fused[0]["engine"] == "bing"
    assert fused[1]["engine"] == "ddg"


def test_fuse_respects_limit():
    many = [{"title": f"T{i}", "url": f"https://e{i}.com/", "snippet": ""} for i in range(6)]
    assert len(_fuse({"bing": many}, limit=3)) == 3


def test_site_results_keeps_host_and_subdomains():
    hits = [
        {"title": "a", "url": "https://www.zhihu.com/question/1", "snippet": ""},
        {"title": "b", "url": "https://zhuanlan.zhihu.com/p/2", "snippet": ""},
        {"title": "off", "url": "https://evil.example/x", "snippet": ""},
    ]
    kept = _site_results(hits, "zhihu.com")
    assert [h["url"] for h in kept] == [
        "https://www.zhihu.com/question/1",
        "https://zhuanlan.zhihu.com/p/2",
    ]
