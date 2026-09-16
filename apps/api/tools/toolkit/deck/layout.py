"""Pure text-measurement geometry, shared by the Visual Compiler.

The engine measures text conservatively (CJK ≈ 1.05em, Latin ≈ 0.52em per char),
wraps to lines, and picks the largest fixed size tier that keeps every block
inside its slot. Slot builders that consume these atoms live in
:mod:`.compiler.layout_engine` (brief-native); this module knows nothing about
any particular IR.

**Semantic preservation (errata #4):** NOTHING is ever trimmed, ellipsized, or
dropped here — full text is laid out or :func:`_fit` raises
:class:`DeckLayoutError` (loud failure). If that happens the construction
guarantee upstream is broken and the job must fail.
"""
from __future__ import annotations

import re

from .errors import DeckLayoutError

# ── page & type constants (MUST mirror deck/templates/slides.typ) ─────────────
PAGE_W_MM, PAGE_H_MM = 338.67, 190.5          # 16:9
MARGIN_X_MM, MARGIN_Y_MM = 16.0, 12.0
CONTENT_W_MM = PAGE_W_MM - 2 * MARGIN_X_MM    # 306.67
CONTENT_H_MM = PAGE_H_MM - 2 * MARGIN_Y_MM    # 166.5
HEADER_H_MM = 26.0                            # title + kicker band on content slides
BODY_H_MM = CONTENT_H_MM - HEADER_H_MM        # the slot every visual fills

_PT_TO_MM = 0.352777
# fixed tiers (pt): display, title, body, caption, micro — mirrors slides.typ
TIERS: dict[str, float] = {"display": 34, "title": 26, "body": 16, "caption": 12, "micro": 10}
FIT_ORDER = ("display", "title", "body", "caption", "micro")   # largest first
LEADING = 1.30                                                   # line height factor

_CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿豈-﫿　-〿＀-￯]")
_LATIN_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9'\-./%]*")


def text_width_mm(text: str, size_pt: float) -> float:
    """Conservative mixed-script width estimate for a single line."""
    em = size_pt * _PT_TO_MM
    w = 0.0
    for ch in text:
        if _CJK.match(ch):
            w += 1.05 * em
        elif ch == " ":
            w += 0.30 * em
        else:
            w += 0.52 * em
    return w


def wrap_lines(text: str, width_mm: float, size_pt: float) -> list[str]:
    """Greedy wrap that preserves EVERY source character (errata #4: no silent edits).

    Atoms are Latin words, single CJK chars, and single other chars (punctuation stays
    attached to its neighbours); a space is emitted only where the source had one.
    CJK breaks anywhere; Latin words stay atomic (an over-long word gets its own line
    rather than being split — measurement accounts for it).
    """
    if not text:
        return []
    atoms: list[tuple[str, bool]] = []          # (token, preceded-by-space in source)
    pending_space = False
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            pending_space = True
            i += 1
            continue
        m = _LATIN_WORD.match(text, i)
        if m:
            atoms.append((m.group(), pending_space))
            i = m.end()
        else:                                    # CJK char or any other single char
            atoms.append((ch, pending_space))
            i += 1
        pending_space = False
    lines: list[str] = []
    cur = ""
    for tok, sp in atoms:
        cand = cur + (" " if sp and cur else "") + tok
        if cur and text_width_mm(cand, size_pt) > width_mm:
            lines.append(cur)
            cur = tok
        else:
            cur = cand
    if cur:
        lines.append(cur)
    return lines


def block_height(lines: int, size_pt: float) -> float:
    return lines * size_pt * _PT_TO_MM * LEADING


def _fit(text: str, width_mm: float, height_mm: float,
         tiers: tuple[str, ...] = FIT_ORDER, max_lines: int | None = None) -> tuple[float, list[str]]:
    """Largest tier whose wrapped block fits the box (or the line cap). Raises when even
    ``micro`` fails — never a silent trim."""
    for name in tiers:
        pt = TIERS[name]
        lines = wrap_lines(text, width_mm, pt)
        if not lines:
            return pt, lines
        if max_lines is not None and len(lines) > max_lines:
            continue
        if block_height(len(lines), pt) <= height_mm + 0.01:
            return pt, lines
    raise DeckLayoutError(
        f"content does not fit even at {FIT_ORDER[-1]} tier "
        f"({text[:40]!r}…): layout must not trim, fix the semantic budget instead"
    )
