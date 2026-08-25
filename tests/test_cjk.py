"""Tests for the jieba CJK keyword channel."""
from rag.cjk import contains_cjk, segment


def test_contains_cjk_detects_chinese():
    assert contains_cjk("什么是注意力机制")
    assert contains_cjk("日本語のテキスト")
    assert contains_cjk("한국어 텍스트")


def test_contains_cjk_false_for_ascii():
    assert not contains_cjk("what is attention")


def test_contains_cjk_true_for_mixed_text():
    assert contains_cjk("混合 english 只有一点")  # mixed still contains CJK


def test_segment_joins_tokens_with_spaces():
    assert segment("什么是注意力机制") == "什么 是 注意力 机制"


def test_segment_empty_and_whitespace():
    assert segment("") == ""
    assert segment("   ") == ""


def test_segment_keeps_latin_tokens():
    out = segment("attention 机制")
    assert "attention" in out
    assert "机制" in out
