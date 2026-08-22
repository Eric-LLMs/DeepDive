"""Tests for the streaming TTS path: sentence splitting (pure function).

``split_sentences`` drives ``/tts/stream`` — it decides how the client's text is chunked into
per-sentence Kokoro calls, so segment boundaries, punctuation retention, and the length cap are
the behaviour that matters offline. (Synthesis itself hits the network, so it isn't tested.)
"""
from core.infrastructure.tts import split_sentences


def test_splits_chinese_on_sentence_punctuation():
    segs = split_sentences("你好。这是一个测试!再试一次?")
    assert segs == ["你好。", "这是一个测试!", "再试一次?"]


def test_splits_english_and_keeps_delimiters():
    segs = split_sentences("Hello. How are you? Fine; great!")
    assert segs == ["Hello.", "How are you?", "Fine;", "great!"]


def test_newlines_are_boundaries():
    # The trailing newline is stripped from the segment (harmless for speech).
    assert split_sentences("第一行\n第二行") == ["第一行", "第二行"]


def test_no_trailing_punctuation_keeps_last_segment():
    assert split_sentences("第一句。没有句号的尾巴") == ["第一句。", "没有句号的尾巴"]


def test_empty_and_whitespace_only_produce_nothing():
    assert split_sentences("") == []
    assert split_sentences("   \n ") == []


def test_oversized_segment_is_hard_split():
    long = "字" * 450
    segs = split_sentences(long)
    assert all(len(s) <= 200 for s in segs)
    assert "".join(segs) == long
