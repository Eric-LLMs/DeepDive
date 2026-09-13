"""Artifact compile service — orchestrates the deterministic core over a real
Research OS project (docs/19 §5/§6/§10, inv. 11).

The default PDF path is fixed by invariant 11: the finalized manuscript (the
edition's ``primary_report_artifact_id`` bytes) projects via
:func:`project_manuscript_to_ast` — the AST is the authoritative content
source; there is NO LLM rewriting, summarization or re-grounding here.
Semantic grounding already happened in the EVIDENCE stage
(``adjudicate_evidence`` wrote verdicts onto the graph); this module only
*carries* the graph Evidence/Source nodes into artifact contracts via the
identity mapping in :mod:`artifact_compiler.mapping` (requirement 6).

Everything stateful rides existing infrastructure: the CAS-locked
:class:`RunStore` for run records, ``ResearchService`` (ACL/tenancy via
``read_project``) for manuscript/graph reads, and the drive's
``save_artifact`` primitive (the same one ``promote_to_drive`` uses) for the
binary PDF. Responses are :class:`ArtifactRef` — never host paths (inv. 9).
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from pathlib import Path
from typing import Any

from artifact_compiler.mapping import graph_citations, graph_evidence, provenance_map
from artifact_compiler.plan import (
    ArtifactMetadata,
    ArtifactPlan,
    SectionPlan,
    SourceScope,
    SummarySpec,
)
from artifact_compiler.preflight import PreflightConfig, PreflightFailure, run_preflight
from artifact_compiler.projection import project_manuscript_to_ast
from artifact_compiler.runstore import PrincipalContext, RunNotFound, RunStore
from artifact_compiler.source import Evidence
from artifact_compiler.states import RunState
from artifact_compiler.typst_compiler import compile_typst, load_default_template, run_typst_compile
from artifact_compiler.validators import (
    ok_or_raise,
    validate_plan_references,
    validate_section_tree,
)
from pydantic import BaseModel, Field

logger = logging.getLogger("artifact.service")

PDF_MIME = "application/pdf"


class ArtifactCompileError(RuntimeError):
    """Honest compile failure. ``run_id`` is set once a run exists (the run has
    then terminalized FAILED_BLOCKED or remains replayable)."""

    def __init__(self, detail: str, *, run_id: str | None = None) -> None:
        super().__init__(detail)
        self.run_id = run_id


class ArtifactRef(BaseModel):
    """The ONLY upward-facing response shape (inv. 9): identity + drive refs +
    manuscript binding. No host filesystem paths."""

    run_id: str
    state: str
    artifact_id: str
    project_id: str
    mime_type: str = PDF_MIME
    pdf_sha256: str = ""
    pdf_size_bytes: int = 0
    drive_asset_id: str | None = None
    drive_path: str | None = None
    outputs_relative_path: str = "outputs/report.pdf"  # scratch-relative, never absolute
    published_from: dict[str, Any] = Field(default_factory=dict)
    manuscript_sha256: str = ""
    provenance_document: str = "provenance.json"


class ArtifactCompileService:
    """One instance per (drive, scratch root); stateless between calls — all
    run state lives in the RunStore."""

    def __init__(
        self,
        *,
        research_service: Any,
        runs_root: Path | str,
        typst_bin: str = "typst",
        typst_timeout_s: float = 60.0,
    ) -> None:
        self._svc = research_service
        self._store = RunStore(Path(runs_root))
        self._typst_bin = typst_bin
        self._typst_timeout_s = typst_timeout_s

    @classmethod
    def for_research(cls, research_service: Any) -> ArtifactCompileService:
        """Wire for an existing ResearchService: ``data/artifact_runs`` as the
        sibling scratch of ``research_scratch`` (docs/19 §2)."""
        return cls(
            research_service=research_service,
            runs_root=Path(research_service.scratch_root).parent / "artifact_runs",
        )

    # ── public API ────────────────────────────────────────────────────────────

    async def compile_project_pdf(
        self, owner_id: uuid.UUID, project_id: str, *, run_id: str | None = None,
    ) -> dict:
        """Compile the edition's finalized manuscript to ``report.pdf`` and
        promote it. Idempotent per project edition: the manuscript hash rides the
        default run id, so a PUBLISH retry replays the same terminal outcome."""
        principal = PrincipalContext.from_uuid(owner_id, project_id)
        project = self._svc.read_project(owner_id, project_id)
        primary = str(project.get("primary_report_artifact_id") or "").strip()
        if not primary:
            raise ArtifactCompileError(
                "no primary report bound — nothing to project (the publish gate "
                "owns this verdict; the compiler never substitutes content)"
            )
        record = self._latest_current_version(owner_id, project_id, primary, project)
        if record is None or not str(record.get("content") or "").strip():
            raise ArtifactCompileError(f"'{primary}' has no current-edition content")
        manuscript: str = record["content"]
        ms_sha = hashlib.sha256(manuscript.encode("utf-8")).hexdigest()
        run_id = run_id or f"pdf-{project_id}-r{project.get('run_seq')}-m{ms_sha[:12]}"

        try:
            existing = self._store.get_run(run_id)
        except RunNotFound:
            existing = None
        if existing is not None:
            return await self._replay_or_run(principal, run_id, existing)
        self._store.create_run(
            principal, run_id=run_id, artifact_id=f"{primary}@pdf",
            metadata={"project_id": project_id, "source_artifact": primary,
                      "source_version": record.get("version"),
                      "manuscript_sha256": ms_sha},
        )
        return await self._run_pipeline(principal, run_id, manuscript, record, project)

    def status(self, owner_id: uuid.UUID, run_id: str) -> dict:
        """Principal-checked run snapshot + committed ArtifactRef (if any)."""
        state = self._store.get_run(run_id)
        if state.get("owner_id") != str(owner_id):
            raise RunNotFound(f"run not found: {run_id}")  # never leak existence
        out: dict[str, Any] = {
            "run_id": run_id, "state": state.get("state"),
            "run_revision": state.get("run_revision"),
            "project_id": state.get("project_id"),
            "last_note": state.get("last_note"),
        }
        try:
            out["artifact_ref"] = self._store.get_document(run_id, "artifact_ref.json")
        except RunNotFound:
            pass
        return out

    # ── internals ─────────────────────────────────────────────────────────────

    def _latest_current_version(
        self, owner_id: uuid.UUID, project_id: str, artifact_id: str, project: dict,
    ) -> dict | None:
        d = self._svc._artifact_dir(owner_id, project_id, artifact_id)
        versions = sorted(
            (int(p.name[1:]) for p in d.glob("v*") if p.name[1:].isdigit()),
        ) if d.is_dir() else []
        for v in reversed(versions):
            rec = self._svc._artifact(owner_id, project_id, artifact_id, v)
            if not self._svc._is_ghost_run_seq(
                rec.get("run_seq"), project.get("run_seq")
            ):
                return rec
        return None

    async def _replay_or_run(
        self, principal: PrincipalContext, run_id: str, state: dict,
    ) -> dict:
        rs = RunState(state["state"])
        if rs is RunState.COMPLETED:
            return self._store.get_document(run_id, "artifact_ref.json")
        if rs in (RunState.NEEDS_REVIEW,):
            return self._ref_view(run_id, state)
        if rs in (RunState.CANCELLED, RunState.BUDGET_EXCEEDED):
            raise ArtifactCompileError(
                f"run {run_id} is terminal {rs.value}; start a new edition run",
                run_id=run_id,
            )
        if rs is RunState.FAILED_BLOCKED:
            raise ArtifactCompileError(
                f"run {run_id} is blocked: {state.get('last_note', '')}",
                run_id=run_id,
            )
        # mid-flight crash rerun: re-drive from QUEUED under a fresh deterministic id
        # would duplicate; instead re-run into a NEW hash-bound id (same manuscript →
        # the same id collides with this stuck run, so bump with a replay counter).
        return await self.compile_project_pdf(
            uuid.UUID(principal.owner_id), principal.project_id or "",
            run_id=f"{run_id}-r{state.get('run_revision', 1)}",
        )

    def _ref_view(self, run_id: str, state: dict) -> dict:
        try:
            return self._store.get_document(run_id, "artifact_ref.json")
        except RunNotFound:
            return {"run_id": run_id, "state": state.get("state"), "artifact_ref": None}

    async def _run_pipeline(
        self, principal: PrincipalContext, run_id: str, manuscript: str,
        record: dict, project: dict,
    ) -> dict:
        try:
            return await self._pipeline_inner(principal, run_id, manuscript, record, project)
        except ArtifactCompileError:
            raise
        except Exception as exc:
            self._block(principal, run_id, f"{type(exc).__name__}: {exc}"[:500])
            raise ArtifactCompileError(str(exc), run_id=run_id) from exc

    async def _pipeline_inner(
        self, principal: PrincipalContext, run_id: str, manuscript: str,
        record: dict, project: dict,
    ) -> dict:
        store = self._store
        hop = RunState.ENV_PREFLIGHT
        store.transition(principal, run_id, hop)

        # 1. ENV_PREFLIGHT — real binaries only; a visuals-free run scopes the mmdc
        #    gate off (config, not spec weakening).
        try:
            report = run_preflight(PreflightConfig(
                typst_bin=self._typst_bin, require_mmdc=False,
            ))
        except PreflightFailure as exc:
            self._block(principal, run_id, f"preflight: {exc}")
            raise ArtifactCompileError(f"preflight failed: {exc}", run_id=run_id) from exc
        store.put_document(principal, run_id, "preflight",
                           {"checks": [{"name": c.name, "ok": c.ok, "detail": c.detail}
                                       for c in report.checks]})

        # 2. EVIDENCE_PROVIDING — read-only upstream (inv. 7): graph Evidence/Source
        #    nodes ride through unchanged; markers are whatever the manuscript carries.
        store.transition(principal, run_id, RunState.EVIDENCE_PROVIDING)
        project_id = principal.project_id or str(project.get("project_id") or "")
        graph = self._svc._load_json(
            self._svc._project_dir(uuid.UUID(principal.owner_id), project_id) / "graph.json",
            {"nodes": [], "edges": []},
        )
        evidence: dict[str, Evidence] = graph_evidence(graph)
        markers, citations = graph_citations(manuscript, evidence)
        if not evidence:
            self._block(principal, run_id, "graph has no Source/Evidence nodes")
            raise ArtifactCompileError(
                "no graph evidence — the PDF must carry provenance; refusing to "
                "render an unsourced report", run_id=run_id,
            )
        store.put_document(principal, run_id, "evidence",
                           {eid: ev.model_dump(mode="json") for eid, ev in evidence.items()})
        store.put_document(principal, run_id, "citations",
                           {cid: c.model_dump(mode="json") for cid, c in citations.items()})
        store.put_document(principal, run_id, "provenance", provenance_map(evidence))
        store.put_document(principal, run_id, "manuscript", {
            "sha256": hashlib.sha256(manuscript.encode("utf-8")).hexdigest(),
            "source_artifact": record["artifact_id"],
            "source_version": record.get("version"),
        })

        # 3-4. PLANNING then WRITING — projection FIRST (pure), plan mirrors it.
        doc = project_manuscript_to_ast(
            manuscript, artifact_id=run_id, citation_markers=markers,
        )
        title = next((b.text for s in doc.sections for b in s.blocks
                      if getattr(b, "level", None) == 1), "Research Report")
        root_id = doc.sections[0].section_id
        section_plans: list[SectionPlan] = []
        for i, s in enumerate(doc.sections):
            head = next((b.text for b in s.blocks if getattr(b, "level", None)), title)
            section_plans.append(SectionPlan(
                section_id=s.section_id,
                parent_id=None if i == 0 else root_id,
                # sibling order is 0-based PER PARENT (validator contract): the
                # root holds order 0, its children restart at 0 — a global
                # counter would start them at 1 and break multi-H1 manuscripts.
                order=0 if i == 0 else i - 1,
                title=head if i == 0 else head or s.section_id,
                content_mode="synthesis",
            ))
        plan = ArtifactPlan(
            artifact_id=run_id,
            metadata=ArtifactMetadata(
                title=title, authors=["DeepDive Research"],
                language=str(project.get("language") or "zh-CN"),
            ),
            source_scope=SourceScope(
                source_ids=sorted({ev.source_id for ev in evidence.values()}),
                evidence_ids=sorted(evidence),
            ),
            summary_spec=SummarySpec(
                purpose="Publication PDF projection of the finalized Research OS manuscript",
            ),
            sections=section_plans,
        )
        store.transition(principal, run_id, RunState.PLANNING)
        store.put_document(principal, run_id, "plan", plan)
        store.transition(principal, run_id, RunState.WRITING)
        store.put_document(principal, run_id, "ast", doc)

        # 5. AST_CONTRACT_QA — deterministic gates; failure is unpatchable here
        #    (the LLM repair authoring lives in the Skill layer, Phase 3).
        store.transition(principal, run_id, RunState.AST_CONTRACT_QA)
        ok_or_raise(validate_section_tree(plan.sections), "section tree")
        ok_or_raise(
            validate_plan_references(plan, set(evidence)), "plan references",
        )
        store.put_document(principal, run_id, "qa_contract", {"ok": True})

        # 6. VISUAL_ENGINE — the projection path carries no visual specs: honest
        #    zero-asset hop (mmdc stays worker-bundled per docs/19 §8).
        store.transition(principal, run_id, RunState.VISUAL_ENGINE)
        store.put_document(principal, run_id, "visuals", {"assets": []})

        # 7. TYPST_COMPILING — pure projection + real CLI; bytes, not JSON.
        store.transition(principal, run_id, RunState.TYPST_COMPILING)
        source_names = {
            ev.source_id: (evidence.get(ev.source_id).locator.url if evidence.get(ev.source_id) else None)
            or ev.evidence_id for ev in evidence.values()
        }
        typst_src = compile_typst(
            plan, doc, template=load_default_template(),
            citations=citations,
            evidence_by_id=evidence, source_names=source_names,
        )
        run_dir = store.run_dir(run_id)
        (run_dir / "report.typ").write_text(typst_src, encoding="utf-8")
        ok, stderr = run_typst_compile(
            typst_src, run_dir, run_dir / "report.pdf",
            typst_bin=self._typst_bin, timeout_s=self._typst_timeout_s,
        )
        pdf_path = run_dir / "report.pdf"
        if not ok or not pdf_path.is_file():
            self._block(principal, run_id, f"typst compile: {stderr[-400:]}")
            raise ArtifactCompileError(
                f"typst compilation failed: {stderr[-400:]}", run_id=run_id,
            )
        pdf_bytes = pdf_path.read_bytes()
        if not pdf_bytes.startswith(b"%PDF"):
            self._block(principal, run_id, "compiler output is not a PDF")
            raise ArtifactCompileError("invalid PDF magic", run_id=run_id)

        # 8. Promotion through the SAME drive primitive promote_to_drive uses; the
        #    Markdown gate already ran upstream and remains the only publish
        #    authority — this is a SIBLING artifact, never a replacement.
        #    Placement (product decision 2026-09-14): a chat task's PDF rides the
        #    TASK's own cloud folder — ``<task folder>/outputs/<name>_v{run_seq}.pdf``
        #    (outputs/ holds publication files only; the reviewed .md now archives
        #    under temp/); a skill-driven project without a cloud folder keeps the
        #    historical ``research/<project_id>/`` layout.
        pdf_name = self._pdf_name(project)
        cloud_root = str(project.get("cloud_folder_path") or "").strip()
        drive_folder = f"{cloud_root}/outputs" if cloud_root else f"research/{project_id}"
        asset = await self._svc.drive.save_artifact(
            uuid.UUID(principal.owner_id),
            name=pdf_name, mime_type=PDF_MIME, content=pdf_bytes,
            folder_path=drive_folder,
        )
        outputs_dir = self._svc._project_dir(uuid.UUID(principal.owner_id), project_id) / "outputs"
        outputs_dir.mkdir(parents=True, exist_ok=True)
        (outputs_dir / pdf_name).write_bytes(pdf_bytes)

        ref = ArtifactRef(
            run_id=run_id, state=RunState.COMPLETED.value,
            artifact_id=f"{record['artifact_id']}@pdf", project_id=project_id,
            pdf_sha256=hashlib.sha256(pdf_bytes).hexdigest(),
            pdf_size_bytes=len(pdf_bytes),
            drive_asset_id=str(asset.id), drive_path=f"{drive_folder}/{asset.name}",
            outputs_relative_path=f"outputs/{pdf_name}",
            published_from={"artifact_id": record["artifact_id"],
                            "version": record.get("version")},
            manuscript_sha256=store.get_document(run_id, "manuscript.json")["sha256"],
        )
        store.put_document(principal, run_id, "artifact_ref", ref)
        state = store.transition(principal, run_id, RunState.COMPLETED,
                                 note=f"pdf {ref.pdf_size_bytes}B sha={ref.pdf_sha256[:12]}")
        assert RunStore.is_publishable(state)
        logger.info("artifact.pdf compiled run=%s drive_asset=%s", run_id, ref.drive_asset_id)
        return ref.model_dump(mode="json")

    def _block(self, principal: PrincipalContext, run_id: str, note: str) -> None:
        """Best-effort hard-fault terminal; never masks the original failure."""
        try:
            self._store.transition(principal, run_id, RunState.FAILED_BLOCKED, note=note)
        except Exception:  # noqa: BLE001
            logger.warning("artifact run %s could not terminalize: %s", run_id, note)

    @staticmethod
    def _pdf_name(project: dict) -> str:
        """Publication file name. A chat task version-names its PDF like the
        edition lineage: ``<task name>_v{run_seq}.pdf`` — the stem is character-
        cleaned and hard-capped at 64 chars so the ``_v{N}.pdf`` tail never pushes
        the name past filesystem component limits. A skill-driven project without
        a cloud task folder keeps the stable ``report.pdf``."""
        if not str(project.get("cloud_folder_path") or "").strip():
            return "report.pdf"
        from plugins.research.plugin import _safe_filename  # local: avoid import cycle
        stem = _safe_filename(str(project.get("name") or "report"))[:64].rstrip(". ")
        return f"{stem or 'report'}_v{int(project.get('run_seq') or 1)}.pdf"
