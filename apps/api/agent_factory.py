"""Composition root for the agent kernel + its shared capability singletons.

Owns everything the interactive chat path and the background agent worker both need, so a
second process never has to reach into ``api.deps`` (FastAPI request plumbing) to build an
:class:`~agent.engine.kernel.AgentKernel`. ``apps/api/deps.py`` and ``apps/worker/tasks.py``
both import from here; ``get_agent_kernel`` is cached per process.

The singletons here are lightweight HTTP/DB clients (llm/embedder/retriever/drive) — no
model is loaded into the process.
"""
from functools import lru_cache
from pathlib import Path

from agent import (
    Context,
    FileMemoryStore,
    PluginManager,
    SkillRegistry,
    ToolRuntime,
    register_builtin_plugins,
)
from agent.engine.kernel import AgentKernel, KernelConfig
from agent.memory.retrieval import RRFMemoryRetriever
from agent.memory.service import MemoryService
from agent.security.approvals import get_approval_bridge
from agent.security.sandbox import Sandbox
from agent.tools.checkpoints import CheckpointStore
from agent.tools.fs_tools import register_fs_tools
from agent.tools.project_context import read_project_context
from api.tools import register_builtin_tools
from api.tools.toolkit import register_toolkit_plugins
from core.application.drive_service import DriveService
from plugins.research.plugin import register_research_plugins
from core.config import export_secret_env, settings
from core.infrastructure.db import SessionLocal
from core.infrastructure.llm import OpenAILLM
from core.infrastructure.memory_retrieval import PgKeywordRecaller, PgVectorRecaller
from core.infrastructure.retrieval_grpc import GrpcRetriever
from core.infrastructure.storage import get_storage
from core.infrastructure.vector import PgVectorStore, TEIEmbedder
from core.infrastructure.web_search import get_web_search_provider
from rag import RAGPipeline, build_pipeline
from rag.query_cache import wrap_retriever

# Lightweight singletons
llm = OpenAILLM()


@lru_cache
def _embedder() -> TEIEmbedder:
    return TEIEmbedder()


@lru_cache
def _batch_embedder() -> TEIEmbedder:
    """Embedder for batch chunk embedding (chat import / learning import).

    The 5s fast-fail ``_embedder`` is for single short query embeddings on the chat path.
    A full chunk batch (default ``embed_batch_size`` × ``ingest_chunk_chars``) measures in
    the tens of seconds against a local TEI, so the batch paths get the same 120s budget
    the ingest worker uses (``apps/worker/settings.py``) instead of timing out.
    """
    return TEIEmbedder(timeout=120.0)


@lru_cache
def _retriever() -> RAGPipeline:
    return build_pipeline(
        embedder=TEIEmbedder(),
        vector_store=PgVectorStore(SessionLocal),
        session_factory=SessionLocal,
        llm=llm,
        settings=settings,
    )


@lru_cache
def _drive_service() -> DriveService:
    return DriveService(SessionLocal)


def get_drive_service() -> DriveService:
    return _drive_service()


def _read_soul() -> str:
    """Load the identity persona (``data/soul.md``), falling back to a one-line persona."""
    soul_path = settings.memory_dir.parent / "soul.md"
    try:
        return soul_path.read_text(encoding="utf-8")
    except OSError:
        return "You are DeepDive, a focused learning-workbench assistant."


@lru_cache
def get_agent_kernel() -> AgentKernel:
    """Build (once per process) the hardened :class:`AgentKernel` composition.

    Shared by the interactive chat path (``apps/api/deps.py``) and the background agent
    worker (``apps/worker/tasks.py``) so a scheduled turn gets the exact same prompt
    assembly, recall, approvals, telemetry, and budget guard as an interactive one.
    """
    # Bridge .env-loaded secrets (e.g. reddit OAuth) into os.environ so standalone
    # plugins discovered from disk can read them; direct env vars still win.
    export_secret_env()
    runtime = ToolRuntime(approval=get_approval_bridge())
    ctx = Context()

    # Retrieval is a capability seam: the tool calls require("retrieval"), so the provider
    # (in-process RAGPipeline or a gRPC client) is swappable via settings.retrieval_mode.
    if settings.retrieval_mode == "grpc":
        ctx.provide(
            "retrieval",
            wrap_retriever(
                GrpcRetriever(
                    settings.retrieval_grpc_addr,
                    token=settings.retrieval_grpc_token,
                    tls_ca=Path(settings.retrieval_grpc_tls_ca)
                    if settings.retrieval_grpc_tls_ca
                    else None,
                )
            ),
        )
    else:
        ctx.provide("retrieval", wrap_retriever(_retriever()))

    ctx.provide("web_search", get_web_search_provider())

    # File-backed tools (pdf_extract_text / pdf_table_to_text) read a drive asset's stored
    # bytes via the shared object storage + a DB session.
    ctx.provide("storage", get_storage())
    ctx.provide("session_factory", SessionLocal)
    # Research OS capabilities: the research plugin injects both before it may mount.
    ctx.provide("drive", get_drive_service())
    ctx.provide("research_scratch", settings.research_scratch_dir)

    # Domain tools first (the kernel registers the core meta-tools on top).
    register_builtin_tools(runtime, ctx, llm)
    register_fs_tools(runtime, settings.workspace_dir)

    skills = SkillRegistry.from_dir(settings.skills_dir)

    # Dual-track memory: session recall = RRF over pgvector + tsvector (tsvector-only when
    # the embedding service is offline — never a silent empty); the file track stays local.
    file_memory = FileMemoryStore(settings.memory_dir)
    retriever = RRFMemoryRetriever(
        keyword=PgKeywordRecaller(SessionLocal),
        vector=PgVectorRecaller(SessionLocal, _embedder()),
    )
    memory = MemoryService(
        file_store=file_memory,
        retriever=retriever,
        memory_md_path=settings.memory_dir / "MEMORY.md",
        note_max_chars=settings.memory_note_max_chars,
    )

    soul = _read_soul()
    sandbox = Sandbox()  # default session = READ-only; WRITE/NETWORK tools are gated

    kernel = AgentKernel(
        llm,
        runtime,
        soul=soul,
        project_context=read_project_context(
            settings.workspace_dir,
            files=settings.project_context_files,
            max_chars=settings.project_context_max_chars,
        ),
        memory=memory,
        skills=skills,
        sandbox=sandbox,
        checkpoints=CheckpointStore(
            settings.workspace_dir, settings.workspace_dir / settings.checkpoint_dir
        ),
        config=KernelConfig(recall_top_k=settings.memory_recall_top_k),
    )

    # rag_search is the learning-corpus retrieval tool; allowlist it so the model
    # always sees its full schema in the tools array from step 0 — no tool_search
    # discovery step required before it can retrieve from imported material.
    kernel.gateway.policy.allow("rag_search")
    # Toolkit content tools are primary user-facing tools: keep them resident too.
    for _toolkit_name in ("summary_gen", "mindmap_gen", "slides_gen"):
        kernel.gateway.policy.allow(_toolkit_name)

    manager = PluginManager(runtime, skills, ctx)
    register_builtin_plugins(manager)
    register_toolkit_plugins(manager, ctx, llm)
    register_research_plugins(manager, ctx)
    manager.discover(settings.plugins_dir)
    return kernel
