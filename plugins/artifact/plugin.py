"""plugins/artifact — thin tool surface for the PDF publish path (docs/19 §10).

One tool, two actions: ``compile_pdf`` (project → finalized manuscript →
deterministic projection → Typst PDF → drive) and ``status`` (run snapshot +
committed ArtifactRef). Argument validation → PrincipalContext →
:class:`ArtifactCompileService`; every rendering/QA/projection decision lives in
Core (``packages/artifact_compiler``). No LLM is reachable from here (inv. 11).
"""
from __future__ import annotations

import json
import uuid
from typing import Any

from agent.engine.decisions import ToolExecution, text_block
from agent.plugins.base import Plugin
from agent.tools.definition import ToolOutput, define_tool
from agent.tools.tool_permissions import ToolPermission
from core.infrastructure.request_context import get_request_user_id

from plugins.artifact.service import ArtifactCompileService


def _render_json(args: dict, value: Any) -> list:
    return [text_block(json.dumps(value, ensure_ascii=False, indent=2, default=str))]


def _current_user() -> uuid.UUID:
    user = get_request_user_id()
    if user is None:
        raise ValueError(
            "artifact tools are tenant-scoped: no request user is set (set_request_user)"
        )
    return user


def _require(args: dict, key: str, action: str) -> Any:
    if key not in args or args[key] is None:
        raise ValueError(f"artifact {action} is missing required argument '{key}'")
    return args[key]


def build_artifact_plugin(ctx: Any | None = None) -> Plugin:
    """Build the artifact plugin with lazy capability resolution (mirrors the
    research plugin: nothing resolves ``drive``/``research_scratch`` at build
    time, so registration is safe before the API provides capabilities)."""

    def service() -> ArtifactCompileService:
        if ctx is None:
            raise RuntimeError("artifact plugin was built without a Context")
        from plugins.research.plugin import ResearchService
        return ArtifactCompileService.for_research(
            ResearchService(
                drive=ctx.resolve("drive"),
                scratch_root=ctx.resolve("research_scratch"),
            ),
        )

    async def _artifact(args: dict, exec: ToolExecution) -> dict:
        action = args["action"]
        if action == "compile_pdf":
            return await service().compile_project_pdf(
                _current_user(),
                _require(args, "project_id", action),
                run_id=args.get("run_id"),
            )
        if action == "status":
            return service().status(
                _current_user(), _require(args, "run_id", action),
            )
        raise ValueError(f"artifact: unknown action {action!r}")

    artifact_tool = define_tool(
        name="artifact",
        description=(
            "Publication PDF for a Research OS project: deterministic projection of "
            "the finalized manuscript (no rewriting) through Typst, promoted to the "
            "drive as report.pdf. Actions: compile_pdf, status."
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["compile_pdf", "status"],
                    "description": "Which action to run. One of: compile_pdf, status.",
                },
                "project_id": {
                    "type": "string",
                    "description": "compile_pdf: research project whose primary manuscript is compiled.",
                },
                "run_id": {
                    "type": "string",
                    "description": "status: the compile run id; compile_pdf: optional replay id override.",
                },
            },
            "required": ["action"],
        },
        output=ToolOutput(schema={"type": "object"}, render=_render_json),
        execute=_artifact,
        is_concurrency_safe=False,
        permission={ToolPermission.READ, ToolPermission.WRITE},
    )

    return Plugin(
        name="artifact",
        description="Artifact Compiler: publication PDF projection over Research OS manuscripts.",
        tools=[artifact_tool],
        inject=["drive", "research_scratch"],
    )


def register_artifact_plugins(manager, ctx: Any | None = None) -> None:
    """Mount the artifact plugin on ``manager`` (used by ``apps/api/agent_factory``)."""
    manager.register(build_artifact_plugin(ctx))
