"""Toolkit content generation: workspace files → slides / mindmap / summary.

Exposes two entry points:

- ``register_toolkit_plugins(manager, ctx, llm)`` — builds and registers the three Cordis
  plugins (``toolkit_slides`` / ``toolkit_mindmap`` / ``toolkit_summary``) onto the agent's
  plugin manager, so the model can call ``slides_gen`` / ``mindmap_gen`` / ``summary_gen``.
- ``pipeline_for(tool, llm)`` — builds a standalone pipeline for the async worker job
  (``/toolkit/generate``), which shares the exact same lifecycle engine.
"""
from __future__ import annotations

from agent.plugins.manager import PluginManager

from .pipeline import TOOLS, ToolKitPipeline
from .plugins import build_toolkit_plugin

__all__ = ["TOOLS", "ToolKitPipeline", "build_toolkit_plugin", "pipeline_for", "register_toolkit_plugins"]


def register_toolkit_plugins(manager: PluginManager, ctx, llm, workspace=None) -> None:
    """Register the three toolkit plugins, hooking their pipelines to the runtime EventBus."""
    events = manager.runtime.events
    for tool in TOOLS:
        manager.register(build_toolkit_plugin(tool, llm, events=events, workspace=workspace))


def pipeline_for(tool: str, llm) -> ToolKitPipeline:
    """Standalone pipeline for the worker job (no EventBus hooks wired)."""
    return ToolKitPipeline(llm, tool)
