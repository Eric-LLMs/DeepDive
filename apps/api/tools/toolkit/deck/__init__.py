"""The grounded visual presentation engine (docs/presentation-engine).

Package map:
  schema.py     — the canonical IR: DocumentRepresentation → GlobalMentalModel →
                  PresentationBrief, budgets + epistemic/traceability invariants
  ingest.py     — multimodal extraction (text blocks + figure slices), zero LLM
  prompts.py    — brief-pass system prompts + wire JSON Schemas + repair prompts
  structured.py — generic structured-LLM engine (wire-slip repair → jsonschema →
                  Pydantic + extra_check → condensed corrective retry)
  workflow_driver.py / workflow_executors.py — the A/B/C/D brief workflow stages
  qa.py         — Layer-1 pure-code gates + the bounded repair matrix
  compiler/     — Visual Compiler: theme, layout engine, deterministic materializer
  layout.py     — shared pure text-measurement geometry (never trims)
  typst_deck.py — pure layout → Typst source emitter (golden-tested)
  templates/    — 16:9 style-only Typst template
  render.py     — Typst compile + RenderReport (uses the artifact_compiler wrapper)
"""
from __future__ import annotations

from .errors import DeckError, DeckLayoutError

__all__ = ["DeckError", "DeckLayoutError"]
