"""CJK keyword channel: jieba segmentation for Chinese/Japanese/Korean text.

``content_search`` on chunks stores jieba-segmented (space-joined) text; the keyword
recaller uses ``to_tsvector('simple', ...)`` over it when the query contains CJK. The
English ``content_en`` tsvector path is untouched, so enabling CJK is additive.
"""
from __future__ import annotations

import re

# CJK Unified Ideographs (中文), Hiragana/Katakana (日本語), Hangul (한국어).
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")


def contains_cjk(text: str) -> bool:
    """True if the text contains any CJK codepoint."""
    return _CJK_RE.search(text) is not None


_segmenter = None


def _get_segmenter():
    """Lazily import jieba (heavy first-load dictionary build) and cache it."""
    global _segmenter
    if _segmenter is None:
        import jieba

        _segmenter = jieba
    return _segmenter


def segment(text: str) -> str:
    """Segment ``text`` with jieba into space-joined tokens.

    Empty/whitespace text returns an empty string. Used both at ingest (store
    ``content_search``) and at query time (segment the CJK query before tsquery).
    """
    text = text.strip()
    if not text:
        return ""
    return " ".join(_get_segmenter().cut(text))
