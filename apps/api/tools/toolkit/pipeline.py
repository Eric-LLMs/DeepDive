"""The shared 5-stage toolkit pipeline: the lifecycle engine behind every tool.

Each run passes through five explicit stages, each with a named Cordis-style hook pair so
other plugins can intercept or observe a stage by registering on the shared :class:`EventBus`
(the same bus the agent's tool lifecycle uses):

    toolkit/before-validate  (waterfall; raise to abort)   toolkit/after-validate  (observer)
    toolkit/before-ingest    (waterfall; raise to abort)   toolkit/after-ingest    (observer)
    toolkit/before-generate  (waterfall; raise to abort)   toolkit/after-generate  (observer)
    toolkit/before-render    (waterfall; raise to abort)   toolkit/after-render    (observer)
    toolkit/before-persist   (waterfall; raise to abort)   toolkit/after-persist   (observer)

Stage order and responsibilities:

1. **validate** — path safety (workspace escape rejected), existence, per-file size cap.
2. **ingest**   — text extraction; the FULL raw text always goes downstream (never a
   digest). ``toolkit_max_input_tokens`` is a capacity check only.
3. **generate** — structured JSON via JSON mode, jsonschema-validated, one retry that
   carries the concrete schema errors back into the prompt. When the complete input
   exceeds the one-shot capacity, the EXPLICIT big-document multi-call flow runs instead:
   one raw-grounded call per batch (:mod:`sources.plan_big_document`) + deterministic
   merge — never a summary as sole input.
4. **render**   — structured JSON → Mermaid / Marp / summary Markdown / .pptx (never raw
   model-written diagram markup).
5. **persist**  — atomic write into the workspace output dir with collision-proof names.

Every stage raises a :class:`ToolKitError` subclass; ``run`` maps any other failure to a
generic :class:`ToolKitError` so callers get one readable message.
"""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from agent.tools.fs_tools import _atomic_write, _resolve
from core.config import settings
from core.infrastructure import media as media_lib

from . import outputs
from .errors import GenerationError, PersistError, SourceError, ToolKitError
from .prompts import SYSTEM_PROMPTS, build_user_prompt
from .sources import (
    WorkspaceSource,
    load_sources,
    plan_big_document,
    remap_citations,
    total_input_tokens,
)

logger = logging.getLogger(__name__)

HOOK_PREFIX = "toolkit"
TOOLS = ("summary", "mindmap", "slides")


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Binary twin of ``fs_tools._atomic_write`` (temp file + os.replace, no torn file)."""
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


@dataclass
class ToolKitResult:
    """The persisted outcome of one pipeline run."""

    tool: str
    files: list[str] = field(default_factory=list)   # absolute paths of written artifacts
    summary: str = ""                                 # one-line human summary
    stats: dict = field(default_factory=dict)         # deck per-stage instrumentation


class ToolKitPipeline:
    """A configured pipeline for one tool (``summary`` / ``mindmap`` / ``slides``)."""

    def __init__(
        self,
        llm,
        tool: str,
        *,
        workspace: Path | None = None,
        events=None,
        output_dir: Path | str | None = None,
    ) -> None:
        if tool not in TOOLS:
            raise ValueError(f"unknown toolkit tool: {tool!r}")
        self.llm = llm
        self.tool = tool
        self.workspace = Path(workspace or settings.workspace_dir)
        # Per-tool default output root under the configured toolkit output dir.
        self.output_dir = (
            Path(output_dir)
            if output_dir is not None
            else Path(settings.toolkit_output_dir) / tool
        )
        self.events = events

    # ── Cordis-style hook firing ──
    async def _hook(self, name: str, *payload) -> None:
        """Waterfall hook: a registered listener can raise to abort the stage."""
        if self.events is not None:
            await self.events.waterfall(f"{HOOK_PREFIX}/{name}", *payload, base=None)

    async def _observe(self, name: str, payload) -> None:
        """Observer hook: listeners run read-only; their errors are logged, never fatal."""
        if self.events is not None:
            await self.events.serial(f"{HOOK_PREFIX}/{name}", payload)

    # ── public entry ──
    async def run(self, paths: list[str], output_dir: str | None = None, **params) -> ToolKitResult:
        """Run the full lifecycle; raises :class:`ToolKitError` subclasses on failure."""
        try:
            return await self._run(paths, output_dir, params)
        except ToolKitError:
            raise
        except Exception as exc:
            raise ToolKitError(f"{self.tool} generation failed: {exc}") from exc

    async def _run(self, paths: list[str], output_dir: str | None, params: dict) -> ToolKitResult:
        await self._hook("before-validate", paths)
        self._deck_stats = {}
        resolved, out_dir = await self.stage_validate(paths, output_dir)
        await self._observe("after-validate", {"paths": [str(p) for p in resolved], "output_dir": str(out_dir)})

        await self._hook("before-ingest", [str(p) for p in resolved])
        sources = await self.stage_ingest(resolved)
        await self._observe("after-ingest", sources)

        # One-shot capacity check (never a compression trigger — see sources.py).
        batches = plan_big_document(sources)
        if batches:
            logger.warning(
                "toolkit %s: input of %d tokens exceeds the %d-token one-shot capacity — "
                "entering the EXPLICIT big-document multi-call flow: %d raw-grounded "
                "call(s), structurally merged; the document is never replaced by a summary",
                self.tool, total_input_tokens(sources),
                settings.toolkit_max_input_tokens, len(batches))

        await self._hook("before-generate", sources)
        data = await self.stage_generate(sources, params, batches=batches)
        await self._observe("after-generate", data)

        await self._hook("before-render", data)
        rendered = await self.stage_render(data)
        await self._observe("after-render", rendered)

        await self._hook("before-persist", rendered)
        result = await self.stage_persist(
            rendered, out_dir, stem=resolved[0].stem if resolved else "artifact",
            bigdoc_calls=len(batches) if batches else None)
        await self._observe("after-persist", result)
        # Per-stage instrumentation (deck only): real LLM calls, provider token counts,
        # seconds and repair events per pass — carried on the result for the job record.
        if getattr(self, "_deck_stats", None) and isinstance(result, ToolKitResult):
            result.stats = self._deck_stats
        return result

    # ── stage 1: validate ──
    async def stage_validate(self, paths: list[str], output_dir: str | None) -> tuple[list[Path], Path]:
        """Path safety + existence + size gates; returns ``(resolved_paths, out_dir)``."""
        if not paths:
            raise SourceError("no files selected")
        resolved: list[Path] = []
        for raw in paths:
            try:
                path = _resolve(self.workspace, raw)
            except ValueError as exc:
                raise SourceError(str(exc)) from exc
            if not path.is_file():
                raise SourceError(f"not a file: {raw}")
            if path.stat().st_size > settings.toolkit_max_file_bytes:
                max_mb = settings.toolkit_max_file_bytes // (1024 * 1024)
                raise SourceError(f"file too large (max {max_mb} MB): {raw}")
            resolved.append(path)

        target = self.output_dir if output_dir is None else Path(output_dir)
        try:
            out_dir = _resolve(self.workspace, str(target))
        except ValueError as exc:
            raise SourceError(f"output dir escapes workspace: {output_dir}") from exc
        return resolved, out_dir

    # ── stage 2: ingest ──
    async def stage_ingest(self, resolved: list[Path]) -> list[WorkspaceSource]:
        """Extract text; the FULL text goes to generation verbatim (no digest/trimming)."""
        return await load_sources(self.workspace, resolved, self.llm)

    # ── stage 3: generate ──
    async def stage_generate(
        self, sources: list[WorkspaceSource], params: dict, *,
        batches: list[list[WorkspaceSource]] | None = None,
    ) -> dict:
        """Structured generation with schema validation and corrective retries.

        ``batches`` (from :func:`sources.plan_big_document`) engages the EXPLICIT
        big-document multi-call flow: one grounding call per batch, each on that batch's
        RAW text, plus a deterministic structural merge. Below capacity (``batches`` None)
        the complete raw text goes to the generator in one call — always verbatim, never
        digested.

        ``slides`` runs the grounded visual presentation engine: multimodal ingest
        (:mod:`.deck.ingest`, zero LLM) → the presentation-brief workflow
        (TEXT → VISUAL → REDUCE → SYNTHESIZE over the generic core,
        :mod:`.deck.workflow_driver`) producing the canonical
        :class:`~.deck.schema.PresentationBrief`. Other tools keep the single
        schema-validated call.

        A per-task custom prompt (``params["prompt"]``, from the generation dialog) is
        appended to the tool's default system prompt, never replacing it — the default
        carries the JSON/schema constraints that keep the pipeline working, and the user's
        own requirements layer on top. An empty/missing prompt uses the default alone.
        """
        if self.tool == "slides":
            from .deck.ingest import build_document_representation
            from .deck.schema import PresentationControls
            from .deck.workflow_driver import run_presentation_workflow

            controls = PresentationControls(
                target_audience=str(params.get("audience") or ""),
                presentation_goal=str(params.get("goal") or ""),
                target_slide_count=int(params["count"]) if params.get("count") else 8,
                language=str(params.get("language") or ""),
                format_mode=str(params.get("format_mode") or "detailed"),
                user_guidance=(params.get("prompt") or "").strip(),
            )
            deck_id = secrets.token_hex(4)
            doc_rep = await build_document_representation(
                sources, workspace=self.workspace, deck_id=deck_id)
            deck_stats: dict = {}
            # The brief chain chunks and grounds internally (per-block Pass A calls on
            # RAW text), so the batch plan is not consumed here.
            brief = await run_presentation_workflow(
                self.llm, doc_rep, controls, deck_id=deck_id, stats_out=deck_stats)
            self._deck_stats = deck_stats
            return {
                "brief": brief.model_dump(mode="json"),
                "document_title": doc_rep.document_title,
                "presentation_goal": controls.presentation_goal,
                "source_names": [s.name for s in sources],
                # the render stage re-embeds referenced slices by asset id (§9.3)
                "visual_assets": [a.model_dump(mode="json")
                                  for a in doc_rep.visual_assets],
            }

        system = SYSTEM_PROMPTS[self.tool]
        custom = (params.get("prompt") or "").strip()
        if custom:
            system = f"{system}\n\n{custom}"
        if not batches:
            prompt = build_user_prompt(self.tool, sources, params)
            return await self._generate_validated(prompt, system)

        partials: list[dict] = []
        n = len(batches)
        for i, batch in enumerate(batches, 1):
            prompt = (
                f"(Big-document batch {i} of {n}: generate this part of the {self.tool} "
                f"strictly from the sources below.)\n\n"
                + build_user_prompt(self.tool, batch, params)
            )
            data = await self._generate_validated(prompt, system)
            # Batch-relative [name:line] citations → absolute original lines before merge.
            partials.append(remap_citations(data, {s.name: s.line_offset for s in batch}))
        return outputs.merge_tool_outputs(self.tool, partials)

    async def _generate_validated(self, prompt: str, system: str) -> dict:
        """One JSON generation + schema validation + single corrective retry."""
        data = await self._complete_json(prompt, system)

        errors = outputs.validate(outputs.SCHEMAS[self.tool], data)
        if errors:
            retry_prompt = (
                "Your previous reply failed JSON schema validation:\n"
                + "\n".join(f"- {e}" for e in errors[:6])
                + "\n\nFix the reply to conform to the schema. Reply with JSON only.\n\n"
                + prompt
            )
            data = await self._complete_json(retry_prompt, system)
            errors = outputs.validate(outputs.SCHEMAS[self.tool], data)
        if errors:
            raise GenerationError("; ".join(errors[:6]))
        return data

    async def _complete_json(self, prompt: str, system: str) -> dict:
        """JSON mode when the client supports it; else a tolerant ``complete`` + parse.

        Toolkit calls run on (near-)full-context inputs: the global per-call timeout is
        too tight for them, so ``toolkit_llm_timeout_s`` bounds each attempt instead.
        """
        timeout = settings.toolkit_llm_timeout_s
        fn = getattr(self.llm, "complete_json", None)
        if fn is not None:
            try:
                return await fn(prompt, system, timeout=timeout)
            except Exception as exc:  # noqa: BLE001 - JSON mode is best-effort; fall back
                logger.info("complete_json unavailable (%s); falling back to tolerant parse", exc)
        raw = await self.llm.complete(prompt, system, timeout=timeout)
        data = outputs.extract_json(raw)
        if data is None:
            raise GenerationError("model response was not valid JSON")
        return data

    # ── stage 4: render ──
    async def stage_render(self, data: dict) -> dict[str, object]:
        """Structured JSON → final display formats (never raw model-written markup).

        Slides render from the canonical :class:`~.deck.schema.PresentationBrief`
        through the Visual Compiler (:mod:`.deck.compiler`): the brief + the sliced
        assets re-emit ``deck.pdf`` with zero LLM calls (§1.2.6), and the compat
        exports (Marp Markdown, .pptx inputs) derive from the same brief.
        ``deck.json`` is the brief itself. The RenderReport gate stays loud —
        nothing silently degrades; template fallbacks surface as ``layout_warnings``.
        """
        if self.tool == "slides":
            import json as _json
            import tempfile
            from pathlib import Path as _P

            from .deck.render import brief_to_marp, brief_to_pptx, render_brief_pdf
            from .deck.schema import PresentationBrief, VisualAsset

            brief = PresentationBrief.model_validate(data["brief"])
            assets = [VisualAsset.model_validate(a)
                      for a in data.get("visual_assets") or []]
            with tempfile.TemporaryDirectory(prefix="deck_") as td:
                res = await asyncio.to_thread(
                    render_brief_pdf, brief, assets, _P(td),
                    document_title=data.get("document_title", ""),
                    source_names=list(data.get("source_names") or []))
            if not res.report.ok:
                raise GenerationError(
                    "deck PDF render failed: "
                    + f"pages {res.report.pages_actual}/{res.report.pages_expected}; "
                    + "; ".join(res.report.typst_warnings[:3]))
            return {
                "deck.pdf": res.pdf,
                "deck.json": _json.dumps(brief.model_dump(mode="json"),
                                         ensure_ascii=False),
                "deck.md": brief_to_marp(
                    brief, document_title=data.get("document_title", "")),
                "deck.pptx": brief_to_pptx(
                    brief, assets,
                    document_title=data.get("document_title", ""),
                    source_names=list(data.get("source_names") or [])),
            }
        return outputs.render(self.tool, data)

    # ── stage 5: persist ──
    async def stage_persist(
        self, rendered: dict[str, object], out_dir: Path, *, stem: str,
        bigdoc_calls: int | None = None,
    ) -> ToolKitResult:
        """Write every rendered artifact atomically; collision-proof names, workspace-confined.

        ``bigdoc_calls`` (set when the explicit big-document multi-call flow ran) is
        surfaced in the one-line summary so the job record states the flow used.
        """
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise PersistError(f"cannot create output dir {out_dir}: {exc}") from exc

        stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S") + "_" + secrets.token_hex(3)
        files: list[str] = []
        for logical, content in rendered.items():
            ext = logical.rsplit(".", 1)[-1]
            filename = f"{stem}_{stamp}.{ext}"
            path = out_dir / filename
            try:
                if isinstance(content, (bytes, bytearray)):
                    await asyncio.to_thread(_atomic_write_bytes, path, bytes(content))
                elif ext == "pptx":
                    # legacy tuple-input path (doc_slides); deck.pptx is real bytes now
                    await asyncio.to_thread(media_lib.build_text_pptx, content, path, title=stem)
                else:
                    await asyncio.to_thread(_atomic_write, path, content)
            except (OSError, ValueError) as exc:  # write error or a broken .pptx
                raise PersistError(f"failed to write {filename}: {exc}") from exc
            files.append(str(path))

        note = (
            f" Large input: {bigdoc_calls} raw grounding calls "
            "(explicit big-document flow)."
            if bigdoc_calls else ""
        )
        return ToolKitResult(
            tool=self.tool,
            files=files,
            summary=f"Generated {self.tool} output ({len(files)} file{'s' if len(files) != 1 else ''}).{note}",
        )
