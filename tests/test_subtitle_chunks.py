"""Subtitle → timestamped chunk contracts (DB-free, no LLM).

Covers the pure cue grouping in ``build_subtitle_chunks`` plus the async wrapper's
config plumbing with contextual/CJK enrichment disabled.
"""
from core.infrastructure import media
from core.infrastructure.ingest import build_subtitle_chunks, parse_subtitle_cues


def _cue(i, start_s, dur_s, text):
    return media.SubtitleCue(i, int(start_s * 1000), int((start_s + dur_s) * 1000), text)


def test_parse_subtitle_cues_dispatch():
    srt = b"1\n00:00:01,000 --> 00:00:03,000\nhello world\n\n2\n00:00:04,000 --> 00:00:06,000\nsecond line\n"
    cues = parse_subtitle_cues(srt, "tomato.srt")
    assert [c.text for c in cues] == ["hello world", "second line"]
    assert parse_subtitle_cues(b"WEBVTT\n\n00:00.000 --> 00:01.000\nhi\n", "x.vtt")[0].start_ms == 0
    assert parse_subtitle_cues(b"[00:01.00]lyric\n", "song.lrc")[0].start_ms == 1000
    assert parse_subtitle_cues(b"plain", "notes.md") is None


def test_groups_fit_chunk_chars():
    cues = [_cue(i, i * 5, 5, "x" * 40) for i in range(1, 6)]
    chunks = build_subtitle_chunks(cues, 100, video_name="tomato_blight")
    # each line 40 chars + newline join: 3 lines = 122 > 100 → groups of 2
    assert [len(c.content_en.split("\n")) for c in chunks] == [2, 2, 1]


def test_meta_carries_video_and_span():
    cues = [_cue(1, 754, 5, "blight lesions on leaves"), _cue(2, 760, 4, "rotate crops yearly")]
    chunks = build_subtitle_chunks(cues, 200, video_name="tomato_blight")
    assert len(chunks) == 1
    m = chunks[0].meta
    assert m["kind"] == "subtitle"
    assert m["video"] == "tomato_blight"
    assert (m["start_ms"], m["end_ms"]) == (754000, 764000)
    assert m["start_ts"] == "0:12:34"
    assert m["end_ts"] == "0:12:44"


def test_group_span_first_to_last():
    cues = [_cue(1, 0, 5, "a"), _cue(2, 10, 5, "b"), _cue(3, 999, 5, "c")]
    chunks = build_subtitle_chunks(cues, 100, video_name="v")
    assert chunks[0].meta["start_ms"] == 0
    assert chunks[0].meta["end_ms"] == 999000 + 5000


def test_oversized_cue_windowed_same_span():
    long = "w" * 250
    cues = [_cue(1, 60, 30, long)]
    chunks = build_subtitle_chunks(cues, 100, video_name="v")
    assert len(chunks) >= 3
    assert all(c.meta["start_ms"] == 60000 and c.meta["end_ms"] == 90000 for c in chunks)


def test_blank_cues_dropped():
    cues = [_cue(1, 0, 5, "real"), _cue(2, 6, 5, "   ")]
    chunks = build_subtitle_chunks(cues, 200, video_name="v")
    assert len(chunks) == 1


def test_empty_cues_no_chunks():
    assert build_subtitle_chunks([], 200, video_name="v") == []
