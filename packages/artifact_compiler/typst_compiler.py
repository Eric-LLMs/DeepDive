"""AST → Typst source: the deterministic pure function (Task 4.2).

``compile_typst`` is *the* golden-snapshot contract: identical inputs must produce
byte-identical ``.typ`` sources, so anything nondeterministic (wall-clock time,
dict iteration of unordered sets, paths) must either be passed in as an explicit
argument or sorted before emission. Rendering decisions made here (citation
numbering order = first appearance in document order) are pure traversals.

Scope note: publication PDF only — no slides/PPTX in this subsystem (docs/research/19 §1).
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal

from artifact_compiler.doc_ast import (
    CalloutBlock,
    ContentBlock,
    DocumentAST,
    FigureBlock,
    HeadingBlock,
    InlineText,
    ListBlock,
    ParagraphBlock,
    SectionAST,
    TableBlock,
)
from artifact_compiler.plan import ArtifactPlan
from artifact_compiler.source import Citation, Evidence

TEMPLATE_PATH = Path(__file__).parent / "templates" / "base.typ"


def load_default_template() -> str:
    """Read the versioned template file. Kept out of ``compile_typst`` so the pure
    function never touches the filesystem."""
    return TEMPLATE_PATH.read_text(encoding="utf-8")


# ── escaping ──────────────────────────────────────────────────────────────────

_SPECIALS = str.maketrans({
    "\\": "\\\\",  # must be first in the map construction? maketrans is a dict — order irrelevant
    "#": "\\#",
    "$": "\\$",
    "@": "\\@",
    "<": "\\<",
    ">": "\\>",
    "[": "\\[",
    "]": "\\]",
    "`": "\\`",
    "*": "\\*",
    "_": "\\_",
})


def esc(text: str) -> str:
    """Make arbitrary authored text literal Typst content (structured emphasis comes
    from the AST, never from characters in the text)."""
    out = text.translate(_SPECIALS)
    # line-start markup tokens are special even after the char-map above
    if out[:1] in ("-", "+", ".", "="):
        out = "\\" + out
    return out


def _string_literal(text: str) -> str:
    """Escape for a Typst ``"..."`` string (used by ``#raw``)."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


# ── citation numbering (document-order first appearance) ─────────────────────

class _RefBook:
    """Assigns 1-based numbers to citation ids by first appearance while walking."""

    def __init__(
        self,
        citations: Mapping[str, Citation],
        evidence_by_id: Mapping[str, Evidence],
        source_names: Mapping[str, str],
    ) -> None:
        self._citations = citations
        self._evidence = evidence_by_id
        self._source_names = source_names
        self.order: list[str] = []  # citation ids, numbered by position

    def register(self, citation_ids: Sequence[str], where: str) -> list[int]:
        nums: list[int] = []
        for cid in citation_ids:
            cit = self._citations.get(cid)
            if cit is None:
                raise ValueError(f"unknown citation id {cid!r} ({where})")
            if cid not in self.order:
                self.order.append(cid)
            nums.append(self.order.index(cid) + 1)
        return nums

    def markers(self, citation_ids: Sequence[str], where: str) -> str:
        if not citation_ids:
            return ""
        nums = sorted(set(self.register(citation_ids, where)))
        return "".join(f"#cnum[{n}]" for n in nums)

    def references(self) -> list[str]:
        lines: list[str] = []
        for n, cid in enumerate(self.order, start=1):
            cit = self._citations[cid]
            parts: list[str] = []
            for eid in cit.evidence_ids:
                ev = self._evidence.get(eid)
                if ev is None:
                    raise ValueError(
                        f"citation {cid!r} references unknown evidence {eid!r} "
                        "(reference integrity must be gated pre-compile)"
                    )
                loc = ev.locator
                anchor = (
                    loc.url or (f"p.{loc.page}" if loc.page is not None else loc.chunk_id)
                )
                name = self._source_names.get(ev.source_id, ev.source_id)
                parts.append(f"{name} ({esc(str(anchor))})")
            label = ", ".join(dict.fromkeys(parts))  # dedupe, insertion-ordered
            lines.append(f"#refEntry([{n}], [{label}])")
        return lines


# ── block renderers ───────────────────────────────────────────────────────────

def _render_inlines(inlines: Sequence[InlineText], refs: _RefBook, where: str) -> str:
    out: list[str] = []
    for it in inlines:
        body = esc(it.text)
        if it.code:
            body = f"#raw(\"{_string_literal(it.text)}\")"
        elif it.bold:
            body = f"#strong([{body}])"
        tail = refs.markers(it.citations or (), where)
        out.append(body + tail)
    return " ".join(out)


def _render_block(b: ContentBlock, refs: _RefBook, depth: int = 0) -> str:
    where = f"block {b.block_id!r}"
    tail = refs.markers(b.citations or (), where)
    if isinstance(b, HeadingBlock):
        # markup-mode heading: "=" * level + space (level 1 => "= Title")
        return f"{'=' * b.level} {esc(b.text)}" + (f"\n{tail}" if tail else "")
    if isinstance(b, ParagraphBlock):
        return _render_inlines(b.inlines, refs, where) + tail
    if isinstance(b, ListBlock):
        bullet = "+" if b.ordered else "-"
        items = "\n".join(
            f"{bullet} " + _render_inlines(item.inlines, refs, where)
            for item in b.items
        )
        return items + (f"\n{tail}" if tail else "")
    if isinstance(b, TableBlock):
        cols = len(b.headers)
        cells = ",\n    ".join(f"[{esc(h)}]" for h in b.headers)
        for row in b.rows:
            padded = list(row[:cols]) + [""] * max(0, cols - len(row))
            cells += ",\n    " + ", ".join(f"[{esc(c)}]" for c in padded)
        head = f"#table(\n    columns: {cols},\n    {cells},\n)"
        if b.caption:
            head = (
                "#figure(\n    table(\n    columns: "
                f"{cols},\n    {cells},\n),\n    caption: [{esc(b.caption)}],\n)"
            )
        return head + (f"\n{tail}" if tail else "")
    if isinstance(b, FigureBlock):
        return (
            f'#figBox("{_string_literal(b.svg_relative_path)}", '
            f"[{esc(b.caption)}])" + (f"\n{tail}" if tail else "")
        )
    if isinstance(b, CalloutBlock):
        inner = "\n".join(_render_block(item, refs, depth + 1) for item in b.body)
        return (
            f"#calloutBox(\"{b.variant}\", [{esc(b.title)}], [\n{inner}\n])"
            + (f"\n{tail}" if tail else "")
        )
    raise ValueError(f"unregistered block type: {type(b).__name__}")


def _render_section(sec: SectionAST, refs: _RefBook) -> str:
    if not sec.blocks:
        raise ValueError(f"section {sec.section_id!r} authored zero blocks")
    first = sec.blocks[0]
    if not (isinstance(first, HeadingBlock) and first.level == 1):
        raise ValueError(
            f"section {sec.section_id!r} must open with a level-1 heading block "
            "(bookmark/outline alignment, docs/research/19 §9 Layer 1)"
        )
    return "\n\n".join(_render_block(b, refs) for b in sec.blocks)


# ── the pure entry point ──────────────────────────────────────────────────────

def compile_typst(
    plan: ArtifactPlan,
    document_ast: DocumentAST,
    *,
    template: str,
    citations: Mapping[str, Citation],
    evidence_by_id: Mapping[str, Evidence],
    source_names: Mapping[str, str] | None = None,
    warning_banner: str | None = None,
    citation_style: Literal["numeric"] = "numeric",
) -> str:
    """Document AST + frozen plan + resolved references → complete ``.typ`` source.

    Raises on any dangling reference (the contract gate must have run first; this is
    the last deterministic wall before the compiler CLI)."""
    if document_ast.artifact_id != plan.artifact_id:
        raise ValueError(
            f"AST artifact_id {document_ast.artifact_id!r} != plan {plan.artifact_id!r}"
        )
    refs = _RefBook(citations, evidence_by_id, source_names or {})

    meta = plan.metadata
    authors = ", ".join(f'"{_string_literal(a)}"' for a in meta.authors)
    keywords = ", ".join(f'"{_string_literal(k)}"' for k in meta.keywords)
    blocks: list[str] = [template.rstrip("\n")]
    blocks.append(
        "#set document(\n"
        f"    title: \"{_string_literal(meta.title)}\",\n"
        f"    author: ({authors}),\n"
        f"    keywords: ({keywords}),\n"
        ")"
    )
    if warning_banner:
        blocks.append(f"#warnBanner[{esc(warning_banner)}]")
    blocks.append("#outline(title: [Contents], depth: 2)\n\n#pagebreak()")

    for sec in document_ast.sections:
        blocks.append(_render_section(sec, refs))

    if refs.order:
        blocks.append(
            "#heading(numbering: none, outlined: false)[References]\n\n"
            + "\n".join(refs.references())
        )

    return "\n\n".join(blocks) + "\n"


# ── the CLI invocation side (impure, kept adjacent but separate) ─────────────

def run_typst_compile(
    typst_source: str,
    workdir: Path,
    out_pdf: Path,
    *,
    typst_bin: str = "typst",
    timeout_s: float = 120.0,
) -> tuple[bool, str]:
    """Write ``report.typ`` under *workdir* (image paths in the source are relative
    to it) and invoke the local ``typst compile`` CLI. Returns ``(ok, stderr)``;
    never raises for compile errors — the driver maps failures to the repair loop."""
    import subprocess

    src = Path(workdir) / "report.typ"
    src.write_text(typst_source, encoding="utf-8")
    try:
        proc = subprocess.run(
            [typst_bin, "compile", str(src), str(out_pdf)],
            capture_output=True, text=True, timeout=timeout_s, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    return proc.returncode == 0 and Path(out_pdf).exists(), proc.stderr or ""
