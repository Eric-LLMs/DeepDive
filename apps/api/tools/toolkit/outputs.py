"""Output schemas, validation, and renderers for the toolkit pipeline.

The model always returns **structured JSON** (JSON mode); the pipeline validates it against
the per-tool schema (jsonschema + a couple of custom constraints), and only then are the
structured objects rendered into the final display formats — Mermaid, Marp Markdown,
summary Markdown, or .pptx. The LLM never writes raw Mermaid/Markdown, so broken fence
markers or unescaped diagram syntax cannot leak through.
"""
from __future__ import annotations

import json
import re

from jsonschema import Draft7Validator

# ── JSON output schemas (the canonical model responses) ──

SUMMARY_SCHEMA: dict = {
    "type": "object",
    "required": ["title", "executive_summary", "key_points", "sections", "qa"],
    "properties": {
        "title": {"type": "string"},
        "executive_summary": {"type": "string"},
        "key_points": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["point", "citations"],
                "properties": {
                    "point": {"type": "string"},
                    "citations": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["heading", "summary", "citations"],
                "properties": {
                    "heading": {"type": "string"},
                    "summary": {"type": "string"},
                    "citations": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "qa": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["question", "answer", "citations"],
                "properties": {
                    "question": {"type": "string"},
                    "answer": {"type": "string"},
                    "citations": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
    },
}

MINDMAP_SCHEMA: dict = {
    "type": "object",
    "required": ["topic", "branches"],
    "properties": {
        "topic": {"type": "string"},
        "branches": {"type": "array", "items": {"$ref": "#/$defs/node"}},
    },
    "$defs": {
        "node": {
            "type": "object",
            "required": ["label", "children"],
            "properties": {
                "label": {"type": "string"},
                "citations": {"type": "array", "items": {"type": "string"}},
                "children": {"type": "array", "items": {"$ref": "#/$defs/node"}},
            },
        }
    },
}

SLIDES_SCHEMA: dict = {
    "type": "object",
    "required": ["title", "slides"],
    "properties": {
        "title": {"type": "string"},
        "slides": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["heading", "core_idea", "support_points", "speaker_notes"],
                "properties": {
                    "heading": {"type": "string"},
                    "core_idea": {"type": "string"},
                    "support_points": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 6,
                        "items": {"type": "string"},
                    },
                    "speaker_notes": {"type": "string"},
                    "citations": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
    },
}

SCHEMAS = {
    "summary": SUMMARY_SCHEMA,
    "mindmap": MINDMAP_SCHEMA,
    "slides": SLIDES_SCHEMA,
}

# Mermaid is hostile to these characters in a bare node label; quote the label when present.
_MM_RESERVED = set("()[]{}:\"")


def validate(schema: dict, obj: object) -> list[str]:
    """Return human-readable validation errors (empty list = valid)."""
    errors = [
        f"{'->'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}"
        for e in Draft7Validator(schema).iter_errors(obj)
    ]
    if schema is MINDMAP_SCHEMA and not errors:
        branches = obj.get("branches") if isinstance(obj, dict) else None
        if branches is not None and _mindmap_depth(branches) > 3:
            errors.append("mindmap nesting exceeds depth 4")
    return errors


def _mindmap_depth(branches: object, level: int = 0) -> int:
    """Deepest branch level (branches themselves are level 1; topic counts as 0)."""
    if not isinstance(branches, list):
        return level
    deepest = level
    for node in branches:
        if isinstance(node, dict):
            deepest = max(deepest, _mindmap_depth(node.get("children", []), level + 1))
    return deepest


def extract_json(raw: str) -> dict | None:
    """Tolerant extraction of a JSON object from a model reply (fences, prose, whitespace)."""
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


# ── Renderers: structured JSON → final display format ──

def render_summary_md(data: dict) -> str:
    """Markdown research summary (executive summary + key points + sections + Q&A)."""
    out: list[str] = [f"# {data.get('title', 'Summary')}", ""]
    out += ["## Executive Summary", "", str(data.get("executive_summary", "")), ""]
    for kp in data.get("key_points", []):
        cite = " ".join(kp.get("citations", []))
        out.append(f"- {kp.get('point', '')}" + (f" {cite}" if cite else ""))
    out.append("")
    for sec in data.get("sections", []):
        out += [f"## {sec.get('heading', '')}", "", str(sec.get("summary", "")), ""]
        cite = " ".join(sec.get("citations", []))
        if cite:
            out += [f"*Sources: {cite}*", ""]
    for qa in data.get("qa", []):
        out += [f"**Q:** {qa.get('question', '')}", "", f"**A:** {qa.get('answer', '')}", ""]
    return "\n".join(out).rstrip() + "\n"


def _mm_label(label: str) -> str:
    """Escape a Mermaid mindmap node label (quote it when it carries reserved characters)."""
    label = (label or "-").strip()
    if not label:
        return '"-"'
    if any(c in label for c in _MM_RESERVED):
        return '"' + label.replace('"', "'") + '"'
    return label


def _mm_root(topic: str) -> str:
    label = (topic or "Mind Map").strip()
    if label and not any(c in label for c in _MM_RESERVED):
        return f"root(({label}))"
    return f'root("{label}")'


def _mm_node(node: dict, depth: int, out: list[str]) -> None:
    out.append("  " * depth + _mm_label(node.get("label", "")))
    for child in node.get("children", []):
        _mm_node(child, depth + 1, out)


def render_mindmap_mmd(data: dict) -> str:
    """Mermaid ``mindmap`` syntax, depth ≤ 4, labels escaped for diagram safety.

    The root is the topmost node; its branches start one indentation level deeper (Mermaid
    mindmap levels follow indentation), so branches are rendered at depth 2.
    """
    out: list[str] = ["mindmap", "  " + _mm_root(data.get("topic", ""))]
    for branch in data.get("branches", []):
        _mm_node(branch, 2, out)
    return "\n".join(out) + "\n"


def render_slides_marp(data: dict) -> str:
    """Marp Markdown deck: one idea per slide, 3 support points, speaker notes."""
    out: list[str] = ["---", "marp: true", "theme: default", "paginate: true", "---", ""]
    out += [f"# {data.get('title', 'Deck')}", ""]
    for s in data.get("slides", []):
        out += ["---", "", f"## {s.get('heading', '')}", ""]
        out += [f"**Core idea:** {s.get('core_idea', '')}", ""]
        for pt in s.get("support_points", []):
            out.append(f"- {pt}")
        cite = " ".join(s.get("citations", []))
        if cite:
            out += ["", f"*Sources: {cite}*"]
        if s.get("speaker_notes"):
            out += ["", f"<!-- Speaker notes: {s['speaker_notes']} -->"]
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def slides_for_pptx(data: dict) -> list[tuple[str, str]]:
    """Extract ``(heading, bullets)`` tuples for :func:`media.build_text_pptx`."""
    slides: list[tuple[str, str]] = []
    for s in data.get("slides", []):
        bullets = "\n".join(s.get("support_points", []))
        slides.append((s.get("heading", ""), bullets))
    return slides


def render(tool: str, data: dict) -> dict[str, object]:
    """Dispatch to the tool's renderer(s); returns ``{logical_name: content}``."""
    if tool == "summary":
        return {"summary.md": render_summary_md(data)}
    if tool == "mindmap":
        return {"mindmap.mmd": render_mindmap_mmd(data)}
    return {"slides.md": render_slides_marp(data), "slides.pptx": slides_for_pptx(data)}
