"""Deterministic manuscript → DocumentAST projection (frozen invariant, docs/
research/19 §3 inv.11).

**The default PDF path is a re-typesetting, never a re-telling.** The finalized
Research OS manuscript (Markdown produced by WRITE/REVIEW, whose semantics this
module must not change) projects onto the AST line by line:

* NO LLM, no summarization, paraphrasing, reordering or content generation;
* textual content is preserved verbatim — unparseable constructs degrade to
  literal text, NEVER to deletion;
* only markdown *syntax* is transformed: ``**x**`` → bold inline, `` `x` `` →
  code inline, pipe rows → TableBlock, fences → code paragraph, ``> `` →
  paragraph, ``[t](u)`` → ``t (u)``, images kept verbatim as literal source;
* provenance rides through: ``citation_markers`` maps verbatim in-text markers
  (e.g. graph anchors ``src:<digest>``) to citation ids, preserving the Research
  OS graph/evidence lineage rather than re-deriving it (inv. 11 / requirement 6);
* LLM rewriting is only ever an explicit, separate workflow and can never
  silently replace this function on the PDF path.

Pure function of its inputs; block ids are content-hash + document-order
counters, so identical manuscripts produce byte-identical ASTs (golden test).
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping

from artifact_compiler.doc_ast import (
    DocumentAST,
    HeadingBlock,
    InlineText,
    ListBlock,
    ListItem,
    ParagraphBlock,
    SectionAST,
    TableBlock,
)

# ── markdown syntax (single source of truth) ─────────────────────────────────

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_BULLET_RE = re.compile(r"^\s*[-*+]\s+(.*)$")
_ORDERED_RE = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_FENCE_RE = re.compile(r"^(```|~~~)")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{3,}.*$")
_QUOTE_RE = re.compile(r"^>\s?(.*)$")
_HR_RE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
_BOLD_RE = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", re.DOTALL)
_CODE_RE = re.compile(r"`([^`]+)`")
_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)\s]*)[^)]*\)")


class ProjectionError(ValueError):
    """Unprojectionable input — fail closed rather than drop content silently."""


class _IdMint:
    """Deterministic block ids: content hash, disambiguated by document-order."""

    def __init__(self) -> None:
        self._used: dict[str, int] = {}

    def mint(self, kind: str, payload: str) -> str:
        base = f"{kind}-{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:10]}"
        n = self._used.get(base, 0)
        self._used[base] = n + 1
        return base if n == 0 else f"{base}-{n}"


# ── inline layer ──────────────────────────────────────────────────────────────

def _match_markers(text: str, markers: tuple[tuple[str, str], ...]) -> list[str]:
    return [cid for lit, cid in markers if lit in text]


def _extract_markers(
    text: str, markers: tuple[tuple[str, str], ...]
) -> tuple[list[str], list[str]]:
    """Split ``text`` at verbatim marker occurrences. Returns (segments, cites):
    the marker substrings themselves are consumed (their meaning is carried by
    the attached citation ids — the sole tolerated syntax removal)."""
    if not markers:
        return [text], []
    hits = sorted(
        ((text.find(lit), lit, cid) for lit, cid in markers if lit in text)
    )
    segments: list[str] = []
    cites: list[str] = []
    pos = 0
    for idx, lit, cid in hits:
        segments.append(text[pos:idx])
        cites.append(cid)
        pos = idx + len(lit)
    segments.append(text[pos:])
    return segments, list(dict.fromkeys(cites))


def _lit(text: str, *, bold: bool = False, code: bool = False) -> InlineText:
    return InlineText(text=text, bold=bold or None, code=code or None)


def _parse_code(text: str, bold: bool) -> list[InlineText]:
    out: list[InlineText] = []
    pos = 0
    for m in _CODE_RE.finditer(text):
        if m.start() > pos:
            out.append(_lit(text[pos:m.start()], bold=bold))
        out.append(_lit(m.group(1), bold=bold, code=True))
        pos = m.end()
    if pos < len(text):
        out.append(_lit(text[pos:], bold=bold))
    return out or [_lit("")]


def _parse_bold(text: str) -> list[InlineText]:
    out: list[InlineText] = []
    pos = 0
    for m in _BOLD_RE.finditer(text):
        if m.start() > pos:
            out.extend(_parse_code(text[pos:m.start()], bold=False))
        out.extend(_parse_code(m.group(1), bold=True))
        pos = m.end()
    if pos < len(text):
        out.extend(_parse_code(text[pos:], bold=False))
    return out or [_lit("")]


def _merge_adjacent(items: list[InlineText]) -> list[InlineText]:
    merged: list[InlineText] = []
    for it in items:
        prev = merged[-1] if merged else None
        if (
            prev is not None
            and prev.bold == it.bold and prev.code == it.code
            and not prev.citations and not it.citations
        ):
            merged[-1] = prev.model_copy(update={"text": prev.text + it.text})
        else:
            merged.append(it)
    return merged


def _parse_inlines(
    text: str,
    markers: tuple[tuple[str, str], ...],
) -> list[InlineText]:
    """Links → markers → bold → code. Every character of the source survives in
    some inline's text (or is carried by an attached citation id)."""
    text = _LINK_RE.sub(
        lambda m: f"{m.group(1)} ({m.group(2)})" if m.group(2) else m.group(1),
        text,
    )
    segments, cites = _extract_markers(text, markers)
    out: list[InlineText] = []
    for seg in segments:
        out.extend(_parse_bold(seg))
    out = _merge_adjacent([x for x in out if x.text])
    if cites:
        if out:
            out[-1] = out[-1].model_copy(update={"citations": cites})
        else:
            out = [InlineText(text="", citations=cites)]
    return out or [_lit("")]


# ── block layer ───────────────────────────────────────────────────────────────

def _table_rows(raw: list[str]) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in raw:
        if _TABLE_SEP_RE.match(line):
            continue
        rows.append([c.strip() for c in line.strip().strip("|").split("|")])
    return rows


def project_manuscript_to_ast(
    markdown: str,
    *,
    artifact_id: str,
    citation_markers: Mapping[str, str] | None = None,
) -> DocumentAST:
    """Project a finalized Research OS manuscript to a DocumentAST (pure function).

    ``citation_markers``: verbatim in-text marker → citation id, supplied by the
    Phase-2 adapter from the claim graph (never guessed here). Raises
    :class:`ProjectionError` on empty input or body text before the first level-1
    heading (the WRITE contract opens reports with a title)."""
    if not markdown.strip():
        raise ProjectionError("empty manuscript — nothing to project")
    markers = tuple(sorted((citation_markers or {}).items()))  # stable iteration
    mint = _IdMint()

    lines = markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    sections: list[SectionAST] = []
    cur_blocks: list = []
    cur_section_id: str | None = None
    para_buf: list[str] = []

    def _require_section() -> None:
        if cur_section_id is None:
            raise ProjectionError(
                "content before the first level-1 heading — the publication "
                "title must open the manuscript (WRITE contract)")

    def flush_para() -> None:
        nonlocal para_buf
        if para_buf:
            text = " ".join(x.strip() for x in para_buf if x.strip())
            para_buf = []
            if text:
                _require_section()
                cur_blocks.append(ParagraphBlock(
                    block_id=mint.mint("p", text),
                    inlines=_parse_inlines(text, markers)))

    def flush_section() -> None:
        nonlocal cur_section_id
        if cur_section_id is not None and cur_blocks:
            sections.append(SectionAST(section_id=cur_section_id, blocks=list(cur_blocks)))
            cur_blocks.clear()

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if _FENCE_RE.match(stripped):            # fenced code → verbatim code inline
            flush_para()
            fence = stripped[:3]
            body: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith(fence):
                body.append(lines[i])
                i += 1
            i += 1
            code_text = "\n".join(body)
            _require_section()
            cur_blocks.append(ParagraphBlock(
                block_id=mint.mint("code", code_text),
                inlines=[_lit(code_text, code=True)] if code_text else [_lit("", code=True)]))
            continue

        if not stripped:
            flush_para()
            i += 1
            continue

        if _HR_RE.match(stripped):               # horizontal rule: layout, no content
            flush_para()
            i += 1
            continue

        if (hm := _HEADING_RE.match(line)):
            flush_para()
            level, title = len(hm.group(1)), hm.group(2)
            if level == 1:
                flush_section()
                cur_section_id = f"sec-{hashlib.sha256(title.encode('utf-8')).hexdigest()[:10]}"
            if cur_section_id is None:
                raise ProjectionError(
                    "content before the first level-1 heading — the publication "
                    "title must open the manuscript (WRITE contract)")
            cur_blocks.append(HeadingBlock(
                block_id=mint.mint("h", title), level=level, text=title))
            i += 1
            continue

        if stripped.startswith("|") and i + 1 < len(lines) and _TABLE_SEP_RE.match(lines[i + 1]):
            flush_para()
            raw = [line]
            j = i + 1
            while j < len(lines) and lines[j].strip().startswith("|"):
                raw.append(lines[j])
                j += 1
            rows = _table_rows(raw)
            _require_section()
            cur_blocks.append(TableBlock(
                block_id=mint.mint("t", raw[0]),
                headers=rows[0] if rows else [],
                rows=rows[1:] if len(rows) > 1 else []))
            i = j
            continue

        if _BULLET_RE.match(line) or _ORDERED_RE.match(line):
            flush_para()
            ordered = bool(_ORDERED_RE.match(line))
            item_re = _ORDERED_RE if ordered else _BULLET_RE
            items: list[ListItem] = []
            while i < len(lines) and (im := item_re.match(lines[i])):
                items.append(ListItem(inlines=_parse_inlines(im.group(1), markers)))
                i += 1
            _require_section()
            cur_blocks.append(ListBlock(
                block_id=mint.mint("l", lines[i - 1]), ordered=ordered, items=items))
            continue

        if (qm := _QUOTE_RE.match(line)):
            flush_para()
            quoted = [qm.group(1)]
            i += 1
            while i < len(lines) and (q2 := _QUOTE_RE.match(lines[i])):
                quoted.append(q2.group(1))
                i += 1
            text = "\n".join(quoted)
            _require_section()
            cur_blocks.append(ParagraphBlock(
                block_id=mint.mint("q", text),
                inlines=_parse_inlines(text, markers)))
            continue

        para_buf.append(line)
        i += 1

    flush_para()
    flush_section()
    if not sections:
        raise ProjectionError("manuscript projected to zero sections — no level-1 heading")
    return DocumentAST(artifact_id=artifact_id, sections=sections)
