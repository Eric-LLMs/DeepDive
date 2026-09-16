"""Theme tokens for the Visual Compiler (§6.1).

Single color source for the deterministic code drawers (:mod:`.materializer`)
and the M2 native PptxCompiler. The Typst template keeps its own palette
constants because it cannot import Python — ``ACADEMIC_LIGHT`` below mirrors
``deck/templates/slides.typ`` value for value (pinned by a compiler test).
"""
from __future__ import annotations

from dataclasses import dataclass

Rgb = tuple[int, int, int]


@dataclass(frozen=True)
class ThemeTokens:
    name: str
    background: Rgb
    surface: Rgb          # chart canvas / slide body
    primary: Rgb          # structural ink, headers
    accent: Rgb           # emphasis, second series
    ink: Rgb              # body text
    muted: Rgb            # secondary text
    faint: Rgb            # citations, captions
    border: Rgb           # strokes
    divider: Rgb          # grid lines
    card: Rgb             # card fill
    band: Rgb             # alt band fill


def _mix(c1: Rgb, c2: Rgb, t: float) -> Rgb:
    """Linear blend, integer-rounded — deterministic, no float dust."""
    return tuple(round(a + (b - a) * t) for a, b in zip(c1, c2))    # type: ignore[return-value]


def _hex(rgb: Rgb) -> str:
    return "#{:02X}{:02X}{:02X}".format(*rgb)


# ── the two shipped themes ────────────────────────────────────────────────────
# ACADEMIC_LIGHT mirrors slides.typ: primary #1F3A5F, accent #C4531B,
# ink #22303C, muted #5B6B7A, faint #8A97A3, line #B9C4CF,
# card #F4F6F8, band #EEF2F6, grid #E1E7EC (chartSlide dashes).
ACADEMIC_LIGHT = ThemeTokens(
    name="academic-light",
    background=(255, 255, 255), surface=(255, 255, 255),
    primary=(31, 58, 95), accent=(196, 83, 27),
    ink=(34, 48, 60), muted=(91, 107, 122), faint=(138, 151, 163),
    border=(185, 196, 207), divider=(225, 231, 236),
    card=(244, 246, 248), band=(238, 242, 246),
)

DARK_BLUEPRINT = ThemeTokens(
    name="dark-blueprint",
    background=(21, 29, 41), surface=(28, 38, 54),
    primary=(126, 166, 212), accent=(226, 133, 84),
    ink=(232, 238, 245), muted=(151, 168, 187), faint=(107, 123, 142),
    border=(57, 72, 94), divider=(45, 60, 80),
    card=(35, 47, 66), band=(26, 35, 50),
)

THEMES: dict[str, ThemeTokens] = {t.name: t for t in (ACADEMIC_LIGHT, DARK_BLUEPRINT)}


def theme_for(style: str) -> ThemeTokens:
    """Map a presentation_style string to a theme. M1 renders Typst in light;
    the mapping matters for materialized PNGs and becomes authoritative in M2."""
    s = (style or "").lower()
    if "blueprint" in s or "dark" in s:
        return DARK_BLUEPRINT
    return ACADEMIC_LIGHT
