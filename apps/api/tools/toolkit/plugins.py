"""The three toolkit tools as Cordis-style :class:`Plugin` objects.

Each tool is one plugin that owns exactly one :class:`ToolDefinition` (``summary_gen`` /
``mindmap_gen`` / ``slides_gen``). The plugin wires a fresh :class:`ToolKitPipeline` for its
tool, exports the pipeline as a named service (``provides``), and attaches a ``tools/result``
observer for audit logging — the plugin lifecycle (mount via the manager, dispose via
unregister, hooks via the shared EventBus) is exactly the Cordis pattern the kernel already
uses.
"""
from __future__ import annotations

import logging

from agent.engine.decisions import ToolExecution, text_block
from agent.plugins.base import Plugin
from agent.plugins.hooks import TOOL_RESULT, observe
from agent.tools.definition import ToolOutput, define_tool
from agent.tools.tool_permissions import ToolPermission

from .pipeline import ToolKitPipeline

logger = logging.getLogger(__name__)

_DESCRIPTIONS = {
    "summary": (
        "Generate a grounded Markdown research summary (executive summary, key points, "
        "per-section analysis, Q&A) from one or more workspace files, with [file.md:line] "
        "citations for every claim."
    ),
    "mindmap": (
        "Generate a Mermaid mind map (.mmd, depth <= 4) from one or more workspace files, "
        "converging on a single central topic."
    ),
    "slides": (
        "Generate a slide deck (Marp Markdown + .pptx) from one or more workspace files: "
        "one core idea per slide, three support points, and speaker notes."
    ),
}

_PLUGIN_DESCRIPTIONS = {
    "summary": "Grounded summary generator (toolkit_summary).",
    "mindmap": "Mermaid mind map generator (toolkit_mindmap).",
    "slides": "Slide deck generator, Marp + .pptx (toolkit_slides).",
}


def _tool_parameters(tool: str) -> dict:
    params: dict = {
        "type": "object",
        "properties": {
            "paths": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string"},
                "description": "Workspace-relative file paths (single or multiple).",
            },
            "output_dir": {
                "type": "string",
                "description": "Optional workspace-relative output directory "
                "(default <toolkit_output_dir>/<tool>).",
            },
        },
        "required": ["paths"],
    }
    if tool == "slides":
        params["properties"]["count"] = {
            "type": "integer",
            "minimum": 3,
            "maximum": 20,
            "description": "Number of slides to generate (3-20, default 8).",
        }
    return params


def _render_result(args: dict, value: dict) -> list:
    lines = [f"Generated **{value['tool']}** output."]
    lines += [f"- {f}" for f in value["files"]]
    return [text_block("\n".join(lines))]


def build_toolkit_plugin(tool: str, llm, events=None, workspace=None) -> Plugin:
    """Build one Cordis-style plugin for ``tool`` (summary / mindmap / slides)."""
    pipeline = ToolKitPipeline(llm, tool, events=events, workspace=workspace)

    async def execute(args: dict, exec: ToolExecution) -> dict:
        params = {k: v for k, v in args.items() if k not in ("paths", "output_dir")}
        result = await pipeline.run(args["paths"], output_dir=args.get("output_dir"), **params)
        # The ToolOutput schema is ``{"type": "object"}``: hand the model a structured
        # dict (tool / files / summary) rather than the internal result dataclass.
        return {"tool": result.tool, "files": result.files, "summary": result.summary}

    tool_def = define_tool(
        name=f"{tool}_gen",
        description=_DESCRIPTIONS[tool],
        parameters=_tool_parameters(tool),
        output=ToolOutput(schema={"type": "object"}, render=_render_result),
        execute=execute,
        is_concurrency_safe=True,
        permission={ToolPermission.READ, ToolPermission.WRITE},
    )

    async def log_outcome(payload: dict) -> None:
        exec_ = payload.get("exec")
        result = payload.get("result")
        state = "error" if getattr(result, "is_error", False) else "ok"
        logger.info("toolkit %s tool=%s -> %s", exec_.name if exec_ else tool, tool, state)

    return Plugin(
        name=f"toolkit_{tool}",
        description=_PLUGIN_DESCRIPTIONS[tool],
        tools=[tool_def],
        # Unique capability name per tool (the Context rejects duplicate providers, and all
        # three pipelines would otherwise collide on a shared "pipeline" capability).
        provides={f"{tool}_pipeline": pipeline},
        listeners=[observe(TOOL_RESULT, log_outcome)],
    )
