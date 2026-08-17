"""Built-in agent tools, one file per tool, auto-discovered at registration time.

Each ``*_tool.py`` module in this package exposes ``register(runtime, ctx, llm)``; the
``__init__`` scans the directory and calls it, so adding a tool is just dropping in a new
file — no central registry edit. Domain tools (rag_search/translate/web_search) live here
rather than in the plugin registry because they depend on gateway-scoped resources: the
retrieval capability seam, the shared LLM client, and the web-search provider.
"""
from __future__ import annotations

import importlib
from pathlib import Path

from agent import Context, ToolRuntime

_REGISTRAR = "register"  # each *tool module exports `register(runtime, ctx, llm) -> None`


def register_builtin_tools(runtime: ToolRuntime, ctx: Context, llm) -> None:
    """Discover and register every ``*_tool`` module in this package."""
    package_dir = Path(__file__).parent
    for module_file in sorted(package_dir.glob("*_tool.py")):
        module_name = f"{__name__}.{module_file.stem}"
        module = importlib.import_module(module_name)
        registrar = getattr(module, _REGISTRAR, None)
        if registrar is not None:
            registrar(runtime, ctx, llm)
