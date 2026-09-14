"""The content-to-slides deck engine (docs/content-to-slides.md).

Package map:
  models.py     — semantic data contracts (Pass A/B/C outputs, DeckSpec, budgets)
  rules.py      — Pass D: pure visual-type fallback chain + budget reporting
  layout.py     — deterministic slot planning (measurement, wrapping, tiers; never trims)
  typst_deck.py — pure DeckSpec+layout → Typst source emitter (golden-tested)
  templates/    — 16:9 style-only Typst template
  render.py     — Typst compile + RenderReport (uses the artifact_compiler wrapper)
  prompts.py    — per-pass system prompts + JSON schemas + prompt builders
  passes.py     — 3 LLM passes (A understand, B outline, C expansion) orchestration
"""
from __future__ import annotations

from .errors import DeckError, DeckLayoutError
from .models import (
    ContentDigest,
    DeckOptions,
    DeckSpec,
    Outline,
    RenderReport,
    Slide,
    VisualPlan,
    check_outline,
)

__all__ = [
    "ContentDigest", "DeckError", "DeckLayoutError", "DeckOptions", "DeckSpec",
    "Outline", "RenderReport", "Slide", "VisualPlan", "check_outline",
]
