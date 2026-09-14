"""Pass A/B/C prompts + wire JSON Schemas for the deck engine (docs §3).

The Schemas here are deliberately *loose* — they bound structure (keys, types, enums,
sanity caps) but leave every real budget to the Pydantic models in :mod:`.models` and
:mod:`.rules`. The pipeline validates against the Schema first (cheap, jsonschema), then
model-loads; both layers feed the same corrective-retry text.

The prompts mirror the closed vocabularies verbatim — a prompt/model drift is a contract
break, so ``tests/test_deck_passes.py`` diffs the enum lists against ``models.py``.
"""
from __future__ import annotations

import json

from ..sources import WorkspaceSource
from .models import KEY_MESSAGE_MAX, TITLE_MAX

PURPOSES = ["PROBLEM", "DEFINITION", "PROCESS", "COMPARISON",
            "TIMELINE", "ARCHITECTURE", "DATA_INSIGHT", "SUMMARY"]
RELATIONSHIPS = ["sequential", "comparative", "hierarchical",
                 "categorical", "quantitative", "singular_takeaway"]
NARRATIVES = ["problem_solution", "concept_map", "chronological",
              "comparative", "top_down"]

_PROVENANCE_SCHEMA = {
    "type": "object",
    "required": ["source_id"],
    "additionalProperties": False,
    "properties": {
        "source_id": {"type": "string"},
        "kind": {"enum": ["session", "document", "book", "subtitles"]},
        # The location fields are optional and may be sent as null (a real model writes
        # "page": null for a line-located fact); ProvenanceRef accepts None for each.
        "message_id": {"type": ["string", "null"]},
        "page": {"type": ["integer", "null"], "minimum": 1},
        "lines": {"type": ["string", "null"]},   # "start" or "start-end"
        "t_ms": {"type": ["integer", "null"], "minimum": 0},
        "quote": {"type": ["string", "null"]},
    },
}

# ── Pass A: content digest ────────────────────────────────────────────────────

DIGEST_SCHEMA = {
    "type": "object",
    "required": ["facts"],
    "additionalProperties": False,
    "properties": {
        "title": {"type": "string"},
        "facts": {
            "type": "array", "minItems": 3, "maxItems": 60,
            "items": {
                "type": "object",
                "required": ["fact_id", "statement", "provenance"],
                "additionalProperties": False,
                "properties": {
                    "fact_id": {"type": "string"},
                    "statement": {"type": "string"},
                    "provenance": {"type": "array", "minItems": 1,
                                   "items": _PROVENANCE_SCHEMA},
                    "superseded_by": {"type": ["string", "null"]},
                },
            },
        },
        # Informational only (no downstream consumer gates on it) — the cap is a
        # sanity bound against bloat, matching the facts cap; Pydantic itself is uncapped.
        "concepts": {"type": "array", "maxItems": 60, "items": {"type": "string"}},
        "quantities": {
            "type": "array", "maxItems": 30,
            "items": {
                "type": "object",
                "required": ["quant_id", "metric", "value", "provenance"],
                "additionalProperties": False,
                "properties": {
                    "quant_id": {"type": "string"},
                    "metric": {"type": "string"},
                    "value": {"type": "number"},
                    "unit": {"type": "string"},
                    "as_of": {"type": "string"},
                    "provenance": {"type": "array", "minItems": 1,
                                   "items": _PROVENANCE_SCHEMA},
                },
            },
        },
    },
}

# ── Pass B: outline (visual-type-pure) ───────────────────────────────────────

OUTLINE_SCHEMA = {
    "type": "object",
    "required": ["title", "narrative_strategy", "sections"],
    "additionalProperties": False,
    "properties": {
        "title": {"type": "string"},
        "narrative_strategy": {"enum": NARRATIVES},
        "sections": {
            "type": "array", "minItems": 1, "maxItems": 6,
            "items": {
                "type": "object",
                "required": ["title", "slides"],
                "additionalProperties": False,
                "properties": {
                    "title": {"type": "string"},
                    "purpose": {"type": "string"},
                    "slides": {
                        "type": "array", "minItems": 1,
                        "items": {
                            "type": "object",
                            "required": ["slide_id", "title", "purpose",
                                         "relationship", "key_message", "fact_refs"],
                            "additionalProperties": False,
                            "properties": {
                                "slide_id": {"type": "string"},
                                "title": {"type": "string"},
                                "purpose": {"enum": PURPOSES},
                                "relationship": {"enum": RELATIONSHIPS},
                                "key_message": {"type": "string"},
                                "fact_refs": {"type": "array", "minItems": 1,
                                              "items": {"type": "string"}},
                            },
                        },
                    },
                },
            },
        },
    },
}

# ── Pass C: slide semantics ───────────────────────────────────────────────────

SLIDE_SCHEMA = {
    "type": "object",
    "required": ["slide_id", "title", "key_message", "purpose",
                 "relationship", "payload"],
    "additionalProperties": False,
    "properties": {
        "slide_id": {"type": "string"},
        "title": {"type": "string"},
        "key_message": {"type": "string"},
        "purpose": {"enum": PURPOSES},
        "relationship": {"enum": RELATIONSHIPS},
        "speaker_notes": {"type": "string"},
        "provenance_refs": {"type": "array", "items": _PROVENANCE_SCHEMA},
        "payload": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "items": {
                    "type": "array", "maxItems": 6,
                    "items": {
                        "type": "object", "required": ["label"],
                        "additionalProperties": False,
                        "properties": {
                            "label": {"type": "string"},
                            "detail": {"type": "string"},
                            "group": {"type": "string"},
                        },
                    },
                },
                "steps": {
                    "type": "array", "maxItems": 8,
                    "items": {
                        "type": "object", "required": ["label"],
                        "additionalProperties": False,
                        "properties": {
                            "label": {"type": "string"},
                            "detail": {"type": "string"},
                            "when": {"type": "string"},
                        },
                    },
                },
                "series": {
                    "type": "array", "maxItems": 2,
                    "items": {
                        "type": "object", "required": ["name", "points"],
                        "additionalProperties": False,
                        "properties": {
                            "name": {"type": "string"},
                            "points": {
                                "type": "array", "minItems": 2, "maxItems": 8,
                                "items": {
                                    "type": "object",
                                    "required": ["x", "y", "quant_ref"],
                                    "additionalProperties": False,
                                    "properties": {
                                        "x": {"type": "string"},
                                        "y": {"type": "number"},
                                        "quant_ref": {"type": "string"},
                                    },
                                },
                            },
                        },
                    },
                },
                "columns": {
                    "type": "array", "minItems": 2, "maxItems": 4,
                    "items": {
                        "type": "object", "required": ["header", "cells"],
                        "additionalProperties": False,
                        "properties": {
                            "header": {"type": "string"},
                            "cells": {"type": "array", "maxItems": 6,
                                      "items": {"type": "string"}},
                        },
                    },
                },
            },
        },
    },
}

SCHEMAS = {"digest": DIGEST_SCHEMA, "outline": OUTLINE_SCHEMA, "slide": SLIDE_SCHEMA}

# ── system prompts ────────────────────────────────────────────────────────────

_COMMON_RULES = (
    "Every factual claim must carry provenance. A provenance ref is an object with "
    "source_id plus EXACTLY ONE location field: lines (\"start\" or \"start-end\") for "
    "documents, page for books, t_ms for subtitles, or message_id for sessions. Use "
    "ONLY those key names — never a \"locator\" key, never extra keys, and omit the "
    "unused location fields (or set them to null). "
    "Never invent facts, numbers, or locations — if the source has no numbers, no quantities. "
    "If a statement is later corrected in a session transcript, keep the corrected fact and "
    "mark the old one with superseded_by. Reply with JSON only."
)

DIGEST_SYSTEM = (
    "You are a grounding-first analyst building a fact base from raw source text. Output "
    "JSON {title, facts:[{fact_id: \"f1\"..., statement, provenance:[...], superseded_by}], "
    "concepts:[\"short bare string\", ...] — plain strings, NEVER {name, description} "
    "objects, quantities:[{quant_id: \"q1\"..., metric, value, unit, provenance:[...]}]}. "
    f"Facts are atomic, checkable statements grounded in the source text below. {_COMMON_RULES}"
)

_UNIT_RULE = (
    "Budget units are counted as: 1 per CJK character, 1 per Latin/digit word; spaces "
    "and punctuation are free (so a 12-unit Chinese title is at most 12 hanzi)."
)

OUTLINE_SYSTEM = (
    "You are a presentation architect. From a FACT BASE (never raw text), design the deck "
    "narrative as JSON {title, narrative_strategy, sections:[{title, purpose, slides:[...]}]}. "
    f"Each slide: slide_id \"s1\"..., title (<= {TITLE_MAX} units), purpose in "
    f"{PURPOSES}, relationship in {RELATIONSHIPS}, key_message (exactly one takeaway, "
    f"<= {KEY_MESSAGE_MAX} units), fact_refs = the fact ids this slide stands on (>= 1). "
    f"{_UNIT_RULE} "
    "Choose NO visual formats — layout is decided downstream. The LAST slide must have "
    "purpose SUMMARY. Reply with JSON only."
)

_SLIDE_PAYLOAD_DOC = (
    "payload carries AT MOST ONE shape, matching the slide's relationship: items "
    "[{label, detail, group?}] (categorical/hierarchical — set group for tiers), "
    "steps [{label, detail, when}] (sequential; when only for timelines), "
    "columns [{header, cells}] (comparative — equal cell counts), "
    "series [{name, points:[{x, y, quant_ref}]}] (quantitative). A singular_takeaway "
    "slide has an empty payload."
)


def slide_system(quant_lines: str) -> str:
    """Pass C system prompt; ``quant_lines`` is the slide-specific allowed quant table."""
    return (
        "You are a slide writer. Expand ONE outline slide into a semantic slide as JSON "
        f"{{slide_id, title, key_message, purpose, relationship, speaker_notes, "
        f"provenance_refs, payload}}. title must be <= {TITLE_MAX} units and key_message "
        f"<= {KEY_MESSAGE_MAX} units. {_UNIT_RULE} "
        f"{_SLIDE_PAYLOAD_DOC} Only these quantities may "
        f"appear in a chart: {quant_lines or '(none — do NOT use series)'}. Chart points "
        "MUST cite one of their quant_refs; any untraceable number is forbidden. "
        f"{_COMMON_RULES}"
    )


# ── user-prompt builders ──────────────────────────────────────────────────────

def build_source_block(sources: list[WorkspaceSource]) -> str:
    """One source text per HTML-comment-headed block (same convention as the toolkit prompts)."""
    parts = []
    for s in sources:
        parts.append(f"<!-- file: {s.name} ({s.line_count} lines) | source_id: {s.name} -->\n{s.text}")
    return "\n\n".join(parts)


def digest_prompt(sources: list[WorkspaceSource], deck_title_hint: str = "") -> str:
    hint = f"\nDeck subject hint: {deck_title_hint}\n" if deck_title_hint else "\n"
    return (
        "Extract the grounded fact base from the sources below." + hint
        + "Use the exact source_id shown in each header for provenance refs.\n\n"
        + build_source_block(sources)
    )


def outline_prompt(digest_json: str, target_slide_count: int,
                   audience: str, goal: str) -> str:
    return (
        f"Design the deck outline. Content slides (excluding cover): exactly "
        f"{target_slide_count} (tolerance ±2).\n"
        + (f"Target audience: {audience}\n" if audience else "")
        + (f"Presentation goal: {goal}\n" if goal else "")
        + "\nFACT BASE (your only input — the raw source is not provided):\n"
        + digest_json
    )


def slide_prompt(outline_item_json: str, facts_json: str, section_title: str) -> str:
    return (
        f"Expand ONE slide (section: {section_title}).\n\n"
        "OUTLINE ITEM:\n" + outline_item_json
        + "\n\nFACTS this slide may use (facts + quantities, the only allowed material):\n"
        + facts_json
        + "\n\nReply with the slide JSON only."
    )


def corrective_retry_prompt(errors: list[str], original_prompt: str) -> str:
    """Shared validate→retry wrapper text (mirrors pipeline.stage_generate's pattern)."""
    return (
        "Your previous reply failed validation:\n"
        + "\n".join(f"- {e}" for e in errors[:6])
        + "\n\nFix exactly these problems. Do not drop content to fit — shorten wording "
        "while preserving meaning, or choose a payload shape that matches the budgets. "
        "Reply with JSON only.\n\n" + original_prompt
    )


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=None)
