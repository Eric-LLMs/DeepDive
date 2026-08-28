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
2. **ingest**   — text extraction + token budget / map-reduce (see :mod:`sources`).
3. **generate** — structured JSON via JSON mode, jsonschema-validated, one retry that
   carries the concrete schema errors back into the prompt.
4. **render**   — structured JSON → Mermaid / Marp / summary Markdown / .pptx (never raw
   model-written diagram markup).
5. **persist**  — atomic write into the workspace output dir with collision-proof names.

Every stage raises a :class:`ToolKitError` subclass; ``run`` maps any other failure to a
generic :class:`ToolKitError` so callers get one readable message.
"""
from __future__ import annotations

import asyncio
import logging
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
from .sources import WorkspaceSource, load_sources

logger = logging.getLogger(__name__)

HOOK_PREFIX = "toolkit"
TOOLS = ("summary", "mindmap", "slides")


@dataclass
class ToolKitResult:
    """The persisted outcome of one pipeline run."""

    tool: str
    files: list[str] = field(default_factory=list)   # absolute paths of written artifacts
    summary: str = ""                                 # one-line human summary


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
        resolved, out_dir = await self.stage_validate(paths, output_dir)
        await self._observe("after-validate", {"paths": [str(p) for p in resolved], "output_dir": str(out_dir)})

        await self._hook("before-ingest", [str(p) for p in resolved])
        sources = await self.stage_ingest(resolved)
        await self._observe("after-ingest", sources)

        await self._hook("before-generate", sources)
        data = await self.stage_generate(sources, params)
        await self._observe("after-generate", data)

        await self._hook("before-render", data)
        rendered = self.stage_render(data)
        await self._observe("after-render", rendered)

        await self._hook("before-persist", rendered)
        result = await self.stage_persist(rendered, out_dir, stem=resolved[0].stem if resolved else "artifact")
        await self._observe("after-persist", result)
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
        """Extract text and apply the token budget / map-reduce."""
        return await load_sources(self.workspace, resolved, self.llm)

    # ── stage 3: generate ──
    async def stage_generate(self, sources: list[WorkspaceSource], params: dict) -> dict:
        """Structured JSON generation with schema validation and one corrective retry.

        A per-task custom prompt (``params["prompt"]``, from the generation dialog) is
        appended to the tool's default system prompt, never replacing it — the default
        carries the JSON/schema constraints that keep the pipeline working, and the user's
        own requirements layer on top. An empty/missing prompt uses the default alone.
        """
        system = SYSTEM_PROMPTS[self.tool]
        custom = (params.get("prompt") or "").strip()
        if custom:
            system = f"{system}\n\n{custom}"
        prompt = build_user_prompt(self.tool, sources, params)
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
        """JSON mode when the client supports it; else a tolerant ``complete`` + parse."""
        fn = getattr(self.llm, "complete_json", None)
        if fn is not None:
            try:
                return await fn(prompt, system)
            except Exception as exc:  # noqa: BLE001 - JSON mode is best-effort; fall back
                logger.info("complete_json unavailable (%s); falling back to tolerant parse", exc)
        raw = await self.llm.complete(prompt, system)
        data = outputs.extract_json(raw)
        if data is None:
            raise GenerationError("model response was not valid JSON")
        return data

    # ── stage 4: render ──
    def stage_render(self, data: dict) -> dict[str, object]:
        """Structured JSON → final display formats (never raw model-written markup)."""
        return outputs.render(self.tool, data)

    # ── stage 5: persist ──
    async def stage_persist(
        self, rendered: dict[str, object], out_dir: Path, *, stem: str
    ) -> ToolKitResult:
        """Write every rendered artifact atomically; collision-proof names, workspace-confined."""
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
                if ext == "pptx":
                    await asyncio.to_thread(media_lib.build_text_pptx, content, path, title=stem)
                else:
                    await asyncio.to_thread(_atomic_write, path, content)
            except (OSError, ValueError) as exc:  # write error or a broken .pptx
                raise PersistError(f"failed to write {filename}: {exc}") from exc
            files.append(str(path))

        return ToolKitResult(
            tool=self.tool,
            files=files,
            summary=f"Generated {self.tool} output ({len(files)} file{'s' if len(files) != 1 else ''}).",
        )
