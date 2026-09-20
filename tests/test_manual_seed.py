"""Product-manual boot seed: version gating, section chunking, public source rows."""
import asyncio
from pathlib import Path

import pytest

from core.infrastructure.manual_seed import (
    MANUAL_DIR,
    MANUAL_VERSION,
    build_manual_chunks,
    seed_product_manual,
    section_blocks,
)

_SAMPLE = """# 测试手册 · 示例

## 小节 A

内容 A,一行说明。

## 小节 B

内容 B,另一行说明。
"""


def test_manual_files_present_and_chinese():
    docs = sorted(MANUAL_DIR.glob("*.md"))
    assert len(docs) >= 8
    for p in docs:
        text = p.read_text(encoding="utf-8")
        assert text.lstrip().startswith("#")  # a heading → section split works
        # every doc carries English UI labels (button names stay ASCII)
        assert any(ch.isascii() and ch.isalpha() for ch in text)


def test_section_blocks_one_per_heading_with_topic_prefix():
    blocks = section_blocks(_SAMPLE)
    assert len(blocks) == 2
    assert blocks[0].startswith("【测试手册 · 示例 · 小节 A】")
    assert "内容 A" in blocks[0]
    assert "内容 B" not in blocks[0]  # topics never mix into one chunk


def test_long_section_is_split():
    text = "# 大\n\n## 节\n\n" + ("句子。" * 500)
    blocks = section_blocks(text)
    assert len(blocks) > 1
    assert all(b.startswith("【大 · 节】") for b in blocks)
    assert all(len(b) < 1400 for b in blocks)


def test_build_manual_chunks_carries_cjk_index():
    chunks = build_manual_chunks(_SAMPLE)
    assert len(chunks) == 2
    for c in chunks:
        assert c.content_search  # jieba segmentation present for the keyword channel
        assert c.chunk_kind == "leaf"


class _SettingStore:
    def __init__(self):
        self.values = {}


class _FakeSession:
    def __init__(self, store):
        self.store = store

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _factory(store):
    def make():
        return _FakeSession(store)
    return make


def test_version_marker_short_circuits(monkeypatch):
    """A boot whose marker matches the version re-seeds nothing."""
    import core.infrastructure.security as sec
    import core.infrastructure.drive_repositories as dr

    store = _SettingStore()
    store.values["manual_seed"] = {"version": MANUAL_VERSION, "docs": ["manual/10-overview"]}

    async def _get(session, key):
        return store.values.get(key)

    async def _boom(*a, **k):  # any deeper call must not happen
        raise AssertionError("re-seeded despite up-to-date marker")

    monkeypatch.setattr(sec, "get_setting", _get)
    monkeypatch.setattr(sec, "set_setting", _boom)
    monkeypatch.setattr(dr.SqlChunkRepository, "delete_by_source", _boom)

    assert asyncio.run(seed_product_manual(_factory(store), embedder=None)) is False
