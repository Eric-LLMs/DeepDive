"""Shared frontmatter parsing (deduplicated for skills + file memory)."""
from agent.frontmatter import parse_frontmatter


def test_no_marker_returns_body_unchanged():
    meta, body = parse_frontmatter("just text\nline two")
    assert meta == {}
    assert body == "just text\nline two"


def test_parses_block_and_body():
    text = "---\nname: hello\ntype: skill\n---\nbody here\nsecond line"
    meta, body = parse_frontmatter(text)
    assert meta == {"name": "hello", "type": "skill"}
    assert body == "body here\nsecond line"


def test_ignores_lines_without_colon():
    text = "---\nname: x\nnot a key\n---\nbody"
    meta, body = parse_frontmatter(text)
    assert meta == {"name": "x"}
    assert body == "body"


def test_colon_inside_value_kept():
    text = '---\ndescription: "a: b"\n---\nbody'
    meta, body = parse_frontmatter(text)
    assert meta == {"description": '"a: b"'}
    assert body == "body"


def test_empty_block():
    meta, body = parse_frontmatter("---\n---\nonly body")
    assert meta == {}
    assert body == "only body"
