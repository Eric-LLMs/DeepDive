"""Pure Typst emitter — DeckSpec + SlideLayouts → deterministic ``.typ`` source (docs §5.2).

The emitter adds NO intelligence: it serializes the layout dicts produced by
:mod:`.layout` into Typst values and appends the deterministic call body to the
style-only template (``templates/slides.typ``). Two identical decks compile to
byte-identical sources — this is what the golden-snapshot test pins.

Contract with the template (see slides.typ header):
  * every ``*_lines`` field is an ALREADY-WRAPPED array of strings — emitted verbatim;
  * every geometry number is unitless mm/pt — the template multiplies by ``1mm``/``1pt``;
  * TEXT_HERO fields are flattened onto the slide dict; all other types nest under
    ``body`` (mirroring how the template functions read them).
"""
from __future__ import annotations

from pathlib import Path

from .layout import CONTENT_W_MM, SlideLayout, wrap_lines
from .models import DeckSpec, ProvenanceRef
from .schema import PresentationBrief

TEMPLATE_PATH = Path(__file__).parent / "templates" / "slides.typ"

# visual type → template function name (must match slides.typ)
SLIDE_FN: dict[str, str] = {
    "TEXT_HERO": "heroSlide",
    "CARDS": "cardsSlide",
    "FLOWCHART": "flowSlide",
    "TIMELINE": "timelineSlide",
    "COMPARISON": "compareSlide",
    "ARCHITECTURE": "archSlide",
    "CHART": "chartSlide",
}


# ── Typst value serialization ─────────────────────────────────────────────────

def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _val(x) -> str:
    """Serialize a python value to a Typst literal (str/number/bool/none/array/dict)."""
    if isinstance(x, bool):                       # bool before int (bool is an int)
        return "true" if x else "false"
    if isinstance(x, str):
        return f'"{_esc(x)}"'
    if isinstance(x, (int, float)):
        return repr(x)                            # unitless; template applies units
    if x is None:
        return "none"
    if isinstance(x, (list, tuple)):
        inner = ", ".join(_val(i) for i in x)
        # a one-element ``(x)`` is a parenthesized value in Typst, not an array —
        # the trailing comma is load-bearing.
        return f"({inner},)" if len(x) == 1 else f"({inner})"
    if isinstance(x, dict):
        inner = ", ".join(f"{k}: {_val(v)}" for k, v in x.items())
        return f"({inner})" if inner else "()"
    raise TypeError(f"cannot emit Typst value for {type(x).__name__}: {x!r}")


# ── citation lines ────────────────────────────────────────────────────────────

def _cite(ref: ProvenanceRef) -> str:
    """One provenance ref → the ``[source:locator]`` convention used across the toolkit."""
    if ref.lines:
        loc = ref.lines
    elif ref.page is not None:
        loc = f"p{ref.page}"
    elif ref.t_ms is not None:
        loc = f"{ref.t_ms // 60000:02d}:{ref.t_ms % 60000 // 1000:02d}"
    elif ref.message_id:
        loc = f"#{ref.message_id[:8]}"
    else:
        loc = ""
    sid = ref.source_id or ref.kind.value
    return f"[{sid}:{loc}]" if loc else f"[{sid}]"


def _citations(slide) -> list[str]:
    out: list[str] = []
    for r in slide.provenance_refs:
        c = _cite(r)
        if c not in out:
            out.append(c)
    return out


# ── the emitter ───────────────────────────────────────────────────────────────

def load_template() -> str:
    return TEMPLATE_PATH.read_text(encoding="utf-8")


def _cover_dict(deck: DeckSpec) -> dict:
    subtitle = " · ".join(x for x in (deck.target_audience, deck.presentation_goal) if x)
    names = [s.name for s in deck.sources]
    return {
        "title_lines": wrap_lines(deck.title, CONTENT_W_MM * 0.85, 34),
        "subtitle_lines": wrap_lines(subtitle, CONTENT_W_MM * 0.85, 15) if subtitle else [],
        "sources_label": "Sources",
        "sources": names,
    }


def _slide_dict(slide, lay: SlideLayout) -> dict:
    d = dict(lay.header)
    if lay.visual_type == "TEXT_HERO":
        d.update(lay.body)                       # hero reads message_*/emphasis directly
    else:
        d["body"] = lay.body
    d["citations"] = _citations(slide)
    return d


def compile_deck_typst(deck: DeckSpec, layouts: list[SlideLayout],
                       template: str | None = None) -> str:
    """Emit the full Typst source: style template + deterministic slide-call body."""
    if len(layouts) != len(deck.slides):
        raise ValueError("layouts must cover every slide, in order")
    tpl = template if template is not None else load_template()

    body: list[str] = ["", _BODY_BANNER]
    body.append(f"#deckCover({_val(_cover_dict(deck))})")
    for slide, lay in zip(deck.slides, layouts):
        fn = SLIDE_FN.get(lay.visual_type)
        if fn is None:                            # pragma: no cover - VisualType closed
            raise ValueError(f"no template function for visual type {lay.visual_type}")
        body.append("#pagebreak()")
        body.append(f"#{fn}({_val(_slide_dict(slide, lay))})")
    return tpl.rstrip() + "\n" + "\n".join(body) + "\n"


# ── brief-native emit (Visual Compiler M1) ────────────────────────────────────
# The layout dicts come from deck.compiler.layout_engine (BriefSlideLayout);
# thesisSlide reads its body FLATTENED onto the slide dict (mirrors heroSlide).

BRIEF_FLAT_FN = frozenset({"thesisSlide"})

_BODY_BANNER = "// ── emitted body (deterministic — do not edit by hand) ──────"


def _brief_cover_dict(brief: PresentationBrief, document_title: str,
                      source_names: list[str]) -> dict:
    subtitle = " · ".join(x for x in (brief.target_audience, brief.presentation_style)
                          if x)
    names = list(source_names) or [brief.deck_id]
    return {
        "title_lines": wrap_lines(document_title or brief.deck_id,
                                  CONTENT_W_MM * 0.85, 34),
        "subtitle_lines": wrap_lines(subtitle, CONTENT_W_MM * 0.85, 15) if subtitle else [],
        "sources_label": "Sources",
        "sources": names,
    }


def compile_brief_typst(brief: PresentationBrief,
                        layouts: list, *, document_title: str = "",
                        source_names: list[str] | None = None,
                        template: str | None = None) -> str:
    """Template + deterministic call body straight from the brief's layouts."""
    if len(layouts) != len(brief.slides):
        raise ValueError("layouts must cover every slide, in order")
    tpl = template if template is not None else load_template()

    body: list[str] = ["", _BODY_BANNER]
    body.append(f"#deckCover({_val(_brief_cover_dict(brief, document_title, source_names or []))})")
    for lay in layouts:
        d = dict(lay.header)
        if lay.fn in BRIEF_FLAT_FN:
            d.update(lay.body)
        else:
            d["body"] = lay.body
        d["citations"] = lay.citations
        body.append("#pagebreak()")
        body.append(f"#{lay.fn}({_val(d)})")
    return tpl.rstrip() + "\n" + "\n".join(body) + "\n"
