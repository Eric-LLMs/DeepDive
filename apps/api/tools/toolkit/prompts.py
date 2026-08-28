"""System prompts + user-prompt builder for the three toolkit tools.

Prompt design follows NotebookLM-style grounded generation: every factual claim carries a
``[file.md:line]`` citation, generation is schema-constrained (the model emits JSON, the
pipeline validates it and renders), and each tool enforces its own density rules:

- **summary**: groundedness first — no hallucination, cite every claim, distinguish the
  core theses from supporting detail.
- **mindmap**: the tree converges on one central topic, depth is capped at 4, and branches
  are short noun phrases (no duplicated sub-branches).
- **slides**: one core idea per slide, exactly three support points, and presenter notes.

All three prompts keep the word "JSON" so providers that enforce ``json_object`` mode are
satisfied, and each ends with "Reply with JSON only."
"""
from __future__ import annotations

from .sources import WorkspaceSource

SUMMARY_SYSTEM = (
    "You are a grounding-first document analyst. Produce a research summary strictly as "
    "JSON with keys {title, executive_summary, key_points:[{point, citations}], "
    "sections:[{heading, summary, citations}], qa:[{question, answer, citations}]}. Every "
    "factual claim MUST cite its source as [file.md:line] using the line markers in the "
    "input; never invent content. Omit a section if the source has no content for it. "
    "Reply with JSON only."
)

MINDMAP_SYSTEM = (
    "You are a knowledge-structure analyst. Turn the document into a mind map strictly as "
    "JSON {topic, branches:[{label, citations, children:[...]}]} with nesting depth at most "
    "4 (topic is depth 1). Each node is a short noun phrase; the tree converges on the "
    "central topic — do not duplicate branches. Each leaf may cite [file.md:line]. "
    "Reply with JSON only."
)

SLIDES_SYSTEM = (
    "You are a slide-deck writer. Produce an outline strictly as JSON {title, "
    "slides:[{heading, core_idea, support_points:[exactly 3 strings], speaker_notes, "
    "citations}]}. ONE core idea per slide, exactly 3 support points, presenter speaker "
    "notes, and citations [file.md:line] per slide. Reply with JSON only."
)

SYSTEM_PROMPTS = {
    "summary": SUMMARY_SYSTEM,
    "mindmap": MINDMAP_SYSTEM,
    "slides": SLIDES_SYSTEM,
}


def build_user_prompt(tool: str, sources: list[WorkspaceSource], params: dict) -> str:
    """Assemble the generation request: sources with line-marker headers + tool nudge.

    Each source is prefixed with an HTML-comment header carrying its file name and line
    count so the model can ground ``[file.md:line]`` citations in the block that follows.
    """
    blocks = []
    for src in sources:
        header = f"<!-- file: {src.name} ({src.line_count} lines) -->"
        blocks.append(f"{header}\n{src.text}")

    body = "\n\n".join(blocks)
    if tool == "slides":
        count = max(3, min(int(params.get("count") or 8), 20))
        return (
            f"Produce {count} slides from the sources below, citing each slide with "
            f"[file.md:line]. Reply with JSON only.\n\n{body}"
        )
    if tool == "mindmap":
        return (
            "Build a mind map (max depth 4) from the sources below, citing [file.md:line]. "
            f"Reply with JSON only.\n\n{body}"
        )
    return (
        "Summarize the sources below, citing [file.md:line] for every factual claim. "
        f"Reply with JSON only.\n\n{body}"
    )
