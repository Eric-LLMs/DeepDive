"""``research``: Phase 0.5 architecture spike — the six Research OS tools.

This is a **spike**, not the Phase 1 implementation: it proves the six frozen tool
contracts (`docs/research/15-cordis-plugin-contract.md`) can be mounted through the real
Cordis DI + ToolRuntime and exercise the six mechanisms (mounting, persistence/crash
recovery, three-layer storage, graph STALE cascade, gate override, idempotency). The
domain logic is deliberately thin and file-backed; a production build replaces
:class:`ResearchService` with the real repositories from the Phase 1 design.

Design notes (spike decisions, all auditable):
- **Factory-built, not discovered.** ``PluginManager.discover`` execs a *fresh* module per
  ``plugin.py``, so a module-level ``PLUGIN`` can never see the API's ``ctx``. Following the
  toolkit pattern (``apps/api/tools/toolkit/plugins.py``), this module exports a *factory*
  ``build_research_plugin(ctx)`` that captures ``ctx`` in tool closures, plus a
  ``register_research_plugins(manager, ctx)`` helper wired from ``apps/api/deps.py``. The
  file deliberately exports **no** ``PLUGIN`` attribute, so ``discover()`` skips it safely.
- **Lazy capability resolution.** Tools resolve ``drive``/``research_scratch`` at *execute*
  time via ``_service_for(ctx)``, not at build time — so the plugin stays a PENDING fiber
  until the capabilities are provided, which is exactly the Cordis mount contract we test.
- **File-backed spike persistence.** Per-owner JSON files under
  ``<research_scratch>/<owner_id>/<project_id>/`` (``project.json``, ``graph.json``,
  ``executions.json``, ``approvals.json``, ``artifacts/<artifact_id>/v<N>``), written with
  atomic ``tmp + os.replace``. No ``workflow_state.json`` is ever written.
- **Tenancy.** The acting user comes from ``core.infrastructure.request_context``
  (``get_request_user_id``); every project is scoped under the owner's directory.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

import portalocker

logger = logging.getLogger(__name__)

from agent.engine.context import current_turn
from agent.engine.decisions import ToolExecution, text_block
from agent.plugins.base import Plugin
from agent.tools.definition import ToolOutput, define_tool
from agent.tools.tool_permissions import ToolPermission
from core.application.drive_service import DriveError
from core.infrastructure.request_context import get_request_user_id

# ── stage / gate vocabulary (frozen in docs/research/07 + 10) ─────────────────
# INBOX is deliberately gone: a project/task is born in DISCOVER (no ghost intake
# state). The agent drives DISCOVER -> ... -> PUBLISH through the six research tools.
_STAGES = [
    "DISCOVER",
    "FRAME",
    "EVIDENCE",
    "DESIGN",
    "EXECUTE",
    "EXPLAIN",
    "WRITE",
    "REVIEW",
    "REPRODUCE",
    "PUBLISH",
]

# Legal transitions (spike subset of the 10-stage DAG).
_LEGAL_NEXT: dict[str, str] = {
    "DISCOVER": "FRAME",
    "FRAME": "EVIDENCE",
    "EVIDENCE": "DESIGN",
    "DESIGN": "EXECUTE",
    "EXECUTE": "EXPLAIN",
    "EXPLAIN": "WRITE",
    "WRITE": "REVIEW",
    "REVIEW": "REPRODUCE",
    "REPRODUCE": "PUBLISH",
    "PUBLISH": None,  # terminal
}

_GATES = ["DESIGN_GATE", "EVIDENCE_GATE", "CLAIM_GATE", "QUALITY_GATE"]

# A transition into ``target`` is guarded by this gate (None = unguarded).
_GATE_BEFORE: dict[str, str | None] = {
    "EXECUTE": "DESIGN_GATE",   # DESIGN -> EXECUTE
    "EXPLAIN": "EVIDENCE_GATE",  # EXECUTE -> EXPLAIN
    "REVIEW": "CLAIM_GATE",      # WRITE -> REVIEW
    "REPRODUCE": "QUALITY_GATE",  # REVIEW -> REPRODUCE
}

# Edge kinds that carry epistemic dependency downstream. ``kind`` flows either
# src -> dst ("produces"/"supports"/...: dst depends on src) or dst -> src
# ("derived_from"/"uses"/...: src depends on dst).
_FORWARD_DEPS = {"generated_by", "produces", "supports", "transformed_by", "tests"}
_REVERSE_DEPS = {"derived_from", "uses", "depends_on", "cites", "motivates", "overrides"}
_INVALIDATES = {"invalidates"}

_VERIFIED = "verified"
_ALLOWED_CLAIM_STRENGTH = {"asserted", "supported", "confident", "contested"}

# ── gate review notes (auto-authored chat explanation, docs/research/10 §6) ──
# When a gate override parks a run for a human decision, the gate service writes one
# deterministic ``system`` note into the task's session chat so the operator sees WHY the
# objective bar was not cleared. Notes are display-only: the ``system`` message row is
# filtered out of model prompt context / session archival / RAG import everywhere.
_GATE_NOTE_KEY = "gate_note_approvals"
_REASON_LIMIT = 1024  # cap on the agent's free-form reason embedded in a note (chars)
_DB_INSERT_ATTEMPTS = 3  # bounded retries before a note is abandoned (marker left unconsumed)
_DB_INSERT_RETRY_DELAY = 0.2  # seconds, linear backoff between attempts

_GATE_LABELS = {
    "DESIGN_GATE": "Design Gate",
    "EVIDENCE_GATE": "Evidence Gate",
    "CLAIM_GATE": "Claim Gate",
    "QUALITY_GATE": "Quality Gate",
}

# Plain-language "what this guards" per gate/check. Only these deterministic checks are
# authoritative; the agent's free-form ``reason`` is secondary context and never overrides them.
_GATE_CHECK_RISKS: dict[str, dict[str, str]] = {
    "EVIDENCE_GATE": {
        "sources_verified": "every source used as fact must exist and be marked verified — an unverified source is not evidence",
        "evidence_linked": "each Evidence item must link to a verified Source, so a claim's support can be traced to an actual source",
        "no_invalid_upstream": "no upstream node may be INVALID — a broken predecessor cannot feed trustworthy evidence",
        "claim_draft_links": "each draft claim destined for the manuscript needs a support link from an Evidence node",
    },
    "DESIGN_GATE": {
        "design_fields": "the Design node must carry register, estimand, identification and risk before the design is locked",
    },
    "CLAIM_GATE": {
        "claims_anchored": "every Claim must carry citations and an allowed strength — restraint is the manuscript bar",
    },
    "QUALITY_GATE": {
        "scorecard": "the quality scorecard needs >=7 dimensions with no fatal finding before a release decision",
    },
}

_GATE_WHY: dict[str, str] = {
    "DESIGN_GATE": "a research design that is not fully specified cannot be executed defensibly",
    "EVIDENCE_GATE": "building the explanation on evidence that does not meet the objective bar would make the conclusions unverifiable",
    "CLAIM_GATE": "claims that are not anchored to registered citations or that overstate the support cannot be published",
    "QUALITY_GATE": "a release decision without a clean scorecard has no objective quality basis",
}


def _trim_reason(reason: str, limit: int = _REASON_LIMIT) -> str:
    """Trim the agent's free-form override reason to ``limit`` chars for safe display."""
    reason = (reason or "").strip()
    if not reason:
        return ""
    if len(reason) <= limit:
        return reason
    return reason[:limit].rstrip() + "…"


def compose_gate_review_note(
    gate_name: str, failed_checks: list[dict], reason: str = ""
) -> str:
    """One deterministic, human-facing note for a gate that failed and awaits an override.

    Pure (no I/O, no state). The note's authority is the mechanical ``failed_checks``; the
    optional agent ``reason`` is appended as plain context, trimmed to ``_REASON_LIMIT``.
    Output is never empty for a recognized gate: when every check is green it says so
    explicitly rather than inventing a failure, so a pending approval always gets a card.
    """
    lines = [f"Review needed — {_GATE_LABELS.get(gate_name, gate_name)} did not pass."]
    lines.append("")
    lines.append(
        "The deterministic gate checks below failed, so this task cannot advance on its own. "
        "This is not a model opinion; it is the mechanical evidence bar the run must clear:"
    )
    risks = _GATE_CHECK_RISKS.get(gate_name, {})
    failed = [c for c in failed_checks if not c.get("ok")]
    if failed:
        lines.append("")
        for check in failed:
            name = check.get("name") or "?"
            lines.append(f"• {name}: {risks.get(name) or 'this gate check did not pass'}")
            if check.get("detail"):
                lines.append(f"  Gate detail: {check['detail']}")
    else:
        lines.append("")
        lines.append("• (the gate's checks are green here — a human decision is still required)")
    lines.append("")
    why = _GATE_WHY.get(gate_name, "a failed gate has not met the objective research bar")
    lines.append(
        f"Why it matters: {why}. Proceeding past a failed check would build the research on "
        "work the gate's objective bar did not accept, so the decision is yours."
    )
    lines.append("")
    lines.append(
        "Approve to continue despite the failed check(s), or Reject and let the agent rework "
        "the underlying research."
    )
    trimmed = _trim_reason(reason)
    if trimmed:
        lines.append("")
        lines.append(f"Agent's request: {trimmed}")
    return "\n".join(lines)


class RevisionConflictError(ValueError):
    """Optimistic-concurrency CAS failure.

    The on-disk ``project_revision`` no longer matches the ``expected_revision`` a caller
    read earlier (another process committed first). The caller must re-read and retry,
    never blind-overwrite — this is the single-writer discipline's backstop.
    """


class ProjectLockError(RuntimeError):
    """Transient: could not acquire the cross-process ``project.json`` lock in time.

    Distinct from a CAS conflict so the driver can grade it Transient (retry with
    backoff) rather than Terminal.
    """


# Seconds to wait for the exclusive project.json lock before raising ProjectLockError.
_PROJECT_LOCK_TIMEOUT = 5.0


def _utc_ms() -> int:
    return int(time.time() * 1000)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _current_user() -> uuid.UUID:
    """The acting tenant (from the request ContextVar). Research tools are tenant-scoped."""
    user = get_request_user_id()
    if user is None:
        raise ValueError(
            "research tools are tenant-scoped: no request user is set (set_request_user)"
        )
    return user


def _safe_filename(name: str) -> str:
    """Strip path/control characters so a user-supplied asset name stays a single filename."""
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name or "file").strip()
    cleaned = cleaned.rstrip(". ")
    return (cleaned or "file")[:120]


# ── ResearchService: spike-grade domain logic ─────────────────────────────────
class ResearchService:
    """Thin, file-backed implementation of the six research tool actions.

    All projects live under ``scratch_root / <owner_id> / <project_id>``. Methods are the
    spike stand-ins for the Phase 1 repositories; the *contracts* they honor (stages, gates,
    cascade, approval PENDING invariant, producer invariant, idempotency) are the frozen
    ``docs/research`` semantics.
    """

    def __init__(self, drive: Any, scratch_root: Path | str) -> None:
        self.drive = drive
        self.scratch_root = Path(scratch_root)

    # ── file helpers ──────────────────────────────────────────────────────
    def _owner_root(self, owner_id: uuid.UUID) -> Path:
        return self.scratch_root / str(owner_id)

    def _resolve_owned_path(self, owner_id: uuid.UUID, *parts: str) -> Path:
        """Join ``*parts`` under the owner's scratch root, rejecting any escape.

        Every user-influenced segment (``project_id``, ``artifact_id``) is resolved
        against the owner root: a ``..`` / absolute-path segment that lands outside
        the owner directory is a ``ValueError`` (traversal denied).
        """
        root = self._owner_root(owner_id).resolve()
        path = root.joinpath(*parts).resolve()
        if not path.is_relative_to(root):
            raise ValueError("path escapes the owner's research scratch root")
        return path

    def _project_dir(self, owner_id: uuid.UUID, project_id: str) -> Path:
        return self._resolve_owned_path(owner_id, project_id)

    @staticmethod
    def _load_json(path: Path, default: Any) -> Any:
        if not path.is_file():
            return default
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _save_json(path: Path, data: Any) -> None:
        """Durably persist ``data``: write ``.tmp`` -> ``fsync`` -> ``os.replace``.

        The tmp + atomic-replace ordering survives a power loss mid-write (either the old
        file or the fully written new file is present, never a torn one); ``fsync`` before
        the rename flushes the bytes to disk. The parent-directory ``fsync`` is best-effort
        (it needs ``O_DIRECTORY``, which Windows lacks).
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            fh.write(json.dumps(data, indent=2, ensure_ascii=False, default=str))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        try:
            dir_fd = os.open(path.parent, os.O_DIRECTORY)  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            return  # Windows / non-posix: no directory fd to fsync
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    def _load_project(self, owner_id: uuid.UUID, project_id: str) -> dict:
        path = self._project_dir(owner_id, project_id) / "project.json"
        project = self._load_json(path, None)
        if project is None:
            raise ValueError(f"project not found: {project_id}")
        return project

    def _save_project(self, project: dict) -> None:
        """Persist a *brand-new* task's ``project.json`` (single writer by construction).

        Only the create paths call this — the task does not exist yet, so no other process
        can be racing on it. Every mutation of an *existing* task's state must go through
        :meth:`atomic_update_project` (the single-writer discipline; see the driver spec).
        """
        project["updated_at"] = _now_iso()
        path = self._project_dir(uuid.UUID(project["owner_id"]), project["id"]) / "project.json"
        self._save_json(path, project)

    def _project_json_path(self, owner_id: uuid.UUID, project_id: str) -> Path:
        return self._project_dir(owner_id, project_id) / "project.json"

    def atomic_update_project(
        self,
        owner_id: uuid.UUID,
        project_id: str,
        mutate_fn,
        *,
        expected_revision: int | None = None,
    ) -> dict:
        """Atomically mutate an existing task's ``project.json`` (single-writer primitive).

        Every visible change to a task's authoritative state goes through this method — never
        a blind ``load -> modify -> save``. A cross-process exclusive file lock (portalocker)
        serializes the API and worker processes; inside the critical section the file is
        re-read fresh, CAS-verified against ``expected_revision`` (when given), mutated by
        ``mutate_fn``, stamped with a new monotonic ``project_revision``, and durably
        persisted (``.tmp`` -> ``fsync`` -> ``os.replace``).

        Raises:
            ValueError: the project does not exist.
            RevisionConflictError: on-disk revision != ``expected_revision``.
            ProjectLockError: the lock was not acquired within the timeout (Transient).
        """
        project_dir = self._project_dir(owner_id, project_id)
        project_dir.mkdir(parents=True, exist_ok=True)
        lock_path = project_dir / ".project.lock"
        path = project_dir / "project.json"
        project: dict | None = None
        try:
            # LOCK_EX | LOCK_NB: non-blocking attempts retried by portalocker until
            # ``timeout`` elapses (a purely blocking lock ignores the timeout — portalocker
            # warns "timeout has no effect in blocking mode" and blocks forever).
            with portalocker.Lock(
                str(lock_path),
                timeout=_PROJECT_LOCK_TIMEOUT,
                check_interval=0.1,
                flags=portalocker.LOCK_EX | portalocker.LOCK_NB,
            ):
                project = self._load_json(path, None)
                if project is None:
                    raise ValueError(f"project not found: {project_id}")
                if (
                    expected_revision is not None
                    and project.get("project_revision", 0) != expected_revision
                ):
                    raise RevisionConflictError(
                        f"project {project_id} revision changed: expected {expected_revision}, "
                        f"got {project.get('project_revision', 0)}"
                    )
                mutate_fn(project)
                project["updated_at"] = _now_iso()
                project["project_revision"] = project.get("project_revision", 0) + 1
                self._save_json(path, project)
        except portalocker.exceptions.AlreadyLocked as exc:
            raise ProjectLockError(
                f"could not lock project {project_id} within {_PROJECT_LOCK_TIMEOUT:.0f}s"
            ) from exc
        return dict(project)

    # ── driver checkpoint (project.json["driver"], read/written atomically) ──
    @staticmethod
    def _empty_driver() -> dict:
        return {
            "run_id": None,             # run_id currently driving (mirrors active_run)
            "turn_index": 0,            # business turn index (0 = interactive first turn)
            "turn_attempt": 1,          # retry count for the current execution (starts at 1)
            "turn_state": "done",       # pending | running | done (see plugins/research/driver.py)
            "execution_id": None,       # "run_id:turn_index:turn_attempt"
            "consecutive_no_progress": 0,
            "cumulative_cost_usd": 0.0,
            "cancel_requested": False,
            "started_at": None,
            "updated_at": None,
            "next_scheduled": None,
        }

    def get_driver_checkpoint(self, owner_id: uuid.UUID, project_id: str) -> dict:
        """The task's driver checkpoint (or the empty default when none is recorded yet)."""
        project = self._load_project(owner_id, project_id)
        driver = project.get("driver")
        if not isinstance(driver, dict):
            return self._empty_driver()
        return {**self._empty_driver(), **driver}

    def set_driver_checkpoint(
        self,
        owner_id: uuid.UUID,
        project_id: str,
        *,
        patch: dict,
        expected_revision: int | None = None,
    ) -> dict:
        """Merge ``patch`` into the driver checkpoint and persist atomically.

        ``expected_revision`` enables optimistic CAS (see the driver: a stale job's write
        must fail rather than clobber a newer one). Returns the fresh driver checkpoint.
        """
        def mutate(project: dict) -> None:
            base = project.get("driver")
            if not isinstance(base, dict):
                base = self._empty_driver()
            merged = {**base, **patch, "updated_at": _now_iso()}
            project["driver"] = merged

        project = self.atomic_update_project(
            owner_id, project_id, mutate, expected_revision=expected_revision
        )
        return dict(project["driver"])

    def request_cancel(
        self, owner_id: uuid.UUID, project_id: str, expected_revision: int | None = None
    ) -> dict:
        """Set ``driver.cancel_requested`` (idempotent) so the loop stops at its next safe point."""
        return self.set_driver_checkpoint(
            owner_id, project_id, patch={"cancel_requested": True},
            expected_revision=expected_revision,
        )

    def read_project_revision(self, owner_id: uuid.UUID, project_id: str) -> int:
        return self._load_project(owner_id, project_id).get("project_revision", 0)

    async def publish_change(
        self, owner_id: uuid.UUID, project_id: str, *, kind: str = "state"
    ) -> int | None:
        """Bump the task's revision and publish a wake-up hint (best-effort, never fatal).

        Called after a mutation that did not itself go through :meth:`atomic_update_project`
        (graph / artifact / execution writes) and by the run driver / chat continuation for
        lifecycle changes. Returns the new ``project_revision``, or ``None`` if the hint
        could not be emitted (the caller's success is never masked).
        """
        from plugins.research.monitor import publish_task_event

        try:
            project = self.atomic_update_project(owner_id, project_id, lambda p: None)
        except Exception as exc:  # noqa: BLE001 - advisory path, never mask the caller
            logger.debug("research publish_change bump failed for %s: %s", project_id, exc)
            return None
        revision = int(project["project_revision"])
        try:
            await publish_task_event(
                project_id, project_revision=revision, kind=kind, ts=_now_iso()
            )
        except Exception as exc:  # noqa: BLE001 - advisory path
            logger.debug("research publish_change event failed for %s: %s", project_id, exc)
        return revision

    # ── read-only helpers for the run driver (plugins/research/driver.py) ──
    def read_project(self, owner_id: uuid.UUID, project_id: str) -> dict:
        """The task's authoritative ``project.json`` (read-only convenience)."""
        return self._load_project(owner_id, project_id)

    def pending_overrides(self, owner_id: uuid.UUID, project_id: str) -> list[dict]:
        """Gate overrides awaiting a human decision (approvals.json ``PENDING``).

        A pending override blocks the auto-run chain (constraint ② of the driver spec):
        the agent requests an override, the driver parks the run until a human resolves it
        instead of silently pressing past the gate.
        """
        approvals = self._load_json(
            self._project_dir(owner_id, project_id) / "approvals.json", {"approvals": []}
        )
        return [a for a in approvals["approvals"] if a.get("status") == "PENDING"]

    async def project_fingerprint(self, owner_id: uuid.UUID, project_id: str) -> dict:
        """Cheap monotone fingerprint of *visible* task progress, for the driver's no-progress gate.

        Compares equal when a turn advanced nothing a user can see: same stage, same passed
        gates, same artifact set (id/version/drive binding), same graph shape, same execution
        count. Cloud-file names are folded in best-effort (a drive outage must never look like
        "progress" or, worse, like a stall — failures just drop the dimension).
        """
        project = self._load_project(owner_id, project_id)
        graph = self._load_graph(owner_id, project_id)
        executions = self._load_json(
            self._project_dir(owner_id, project_id) / "executions.json", {"executions": []}
        )
        fingerprint = {
            "stage": project.get("stage"),
            "gates": {
                k: v for k, v in (project.get("gates") or {}).items()
                if v in ("PASS", "OVERRIDE")
            },
            "artifacts": sorted(
                (a["artifact_id"], a["version"], a.get("drive_asset_id"))
                for a in self.list_artifacts(owner_id, project_id)
            ),
            "nodes": len(graph.get("nodes", [])),
            "edges": len(graph.get("edges", [])),
            "executions": len(executions.get("executions", [])),
        }
        cloud = project.get("cloud_folder_path")
        if cloud:
            try:
                files = await self.drive.list_files(owner_id)
                fingerprint["cloud_files"] = sorted(
                    f["name"]
                    for f in files
                    if (f["folder_path"] or "").startswith(f"{cloud}/")
                )
            except Exception:  # noqa: BLE001 - best-effort dimension, never fatal
                logger.debug("research fingerprint cloud listing failed for %s", project_id)
        return fingerprint

    def _load_graph(self, owner_id: uuid.UUID, project_id: str) -> dict:
        path = self._project_dir(owner_id, project_id) / "graph.json"
        return self._load_json(path, {"nodes": [], "edges": []})

    def _save_graph(self, owner_id: uuid.UUID, project_id: str, graph: dict) -> None:
        self._save_json(self._project_dir(owner_id, project_id) / "graph.json", graph)

    def _artifact_dir(self, owner_id: uuid.UUID, project_id: str, artifact_id: str) -> Path:
        return self._resolve_owned_path(owner_id, project_id, "artifacts", artifact_id)

    def _artifact(self, owner_id: uuid.UUID, project_id: str, artifact_id: str, version: int) -> dict:
        path = self._artifact_dir(owner_id, project_id, artifact_id) / f"v{version}"
        record = self._load_json(path, None)
        if record is None:
            raise ValueError(f"artifact not found: {artifact_id} v{version}")
        return record

    # ── research_project ──────────────────────────────────────────────────
    async def create_project(
        self,
        owner_id: uuid.UUID,
        *,
        name: str,
        profile: str,
        idempotency_key: str | None = None,
    ) -> dict:
        if idempotency_key:
            existing = self._find_by_idempotency(owner_id, "project", idempotency_key)
            if existing is not None:
                return {
                    "project_id": existing["id"],
                    "name": existing["name"],
                    "owner_id": str(owner_id),
                    "stage": existing["stage"],
                    "status": existing["status"],
                    "profile": existing["profile"],
                    "idempotent": True,
                }
        project = {
            "id": str(uuid.uuid4()),
            "owner_id": str(owner_id),
            "name": name,
            "profile": profile,
            "status": "ACTIVE",
            "stage": "DISCOVER",
            "gates": {gate: "NOT_RUN" for gate in _GATES},
            "idempotency_key": idempotency_key,
            "project_revision": 0,      # monotonic version; bumped by every atomic commit
            "driver": None,             # driver checkpoint (materialized on first run)
            "last_block": None,         # terminal-stop record {kind, reason, at, execution_id}
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
        }
        self._save_project(project)
        self._save_graph(owner_id, project["id"], {"nodes": [], "edges": []})
        return {
            "project_id": project["id"],
            "name": name,
            "owner_id": str(owner_id),
            "stage": "DISCOVER",
            "status": "ACTIVE",
            "profile": profile,
            "idempotent": False,
        }

    def _find_by_idempotency(
        self, owner_id: uuid.UUID, kind: str, idempotency_key: str
    ) -> dict | None:
        owner_dir = self._owner_root(owner_id)
        if not owner_dir.is_dir():
            return None
        for project_dir in owner_dir.iterdir():
            project = self._load_json(project_dir / "project.json", None)
            if project is None:
                continue
            if kind == "project":
                if project.get("idempotency_key") == idempotency_key:
                    return project
            elif kind == "artifact":
                artifacts = (project_dir / "artifacts").iterdir() if (project_dir / "artifacts").is_dir() else []
                for artifact_dir in artifacts:
                    for version_file in artifact_dir.glob("v*"):
                        record = self._load_json(version_file, None)
                        if record and record.get("idempotency_key") == idempotency_key:
                            return record
        return None

    def resume_project(self, owner_id: uuid.UUID, project_id: str) -> dict:
        project = self._load_project(owner_id, project_id)
        return {
            "project_id": project["id"],
            "name": project["name"],
            "owner_id": project["owner_id"],
            "status": project["status"],
            "stage": project["stage"],
            "profile": project["profile"],
            "gates": project["gates"],
            "updated_at": project["updated_at"],
        }

    def snapshot_project(self, owner_id: uuid.UUID, project_id: str) -> dict:
        project = self._load_project(owner_id, project_id)
        graph = self._load_graph(owner_id, project_id)
        return {
            "project_id": project["id"],
            "snapshot_at": _now_iso(),
            "stage": project["stage"],
            "gates": project["gates"],
            "node_count": len(graph["nodes"]),
            "edge_count": len(graph["edges"]),
        }

    def archive_project(self, owner_id: uuid.UUID, project_id: str) -> dict:
        def mutate(project: dict) -> None:
            project["status"] = "ARCHIVED"

        project = self.atomic_update_project(owner_id, project_id, mutate)
        return {"project_id": project["id"], "status": "ARCHIVED"}

    # ── research_artifact ─────────────────────────────────────────────────
    async def write_scratch(
        self,
        owner_id: uuid.UUID,
        project_id: str,
        *,
        artifact_id: str,
        content: str,
        idempotency_key: str | None = None,
        generated_by_execution: str | None = None,
    ) -> dict:
        project = self._load_project(owner_id, project_id)
        if idempotency_key:
            existing = self._find_by_idempotency(owner_id, "artifact", idempotency_key)
            if existing is not None:
                return {
                    "artifact_id": existing["artifact_id"],
                    "project_id": project_id,
                    "version": existing["version"],
                    "status": existing["status"],
                    "generated_by_execution": existing.get("generated_by_execution"),
                    "created_by": existing.get("created_by"),
                    "idempotent": True,
                }
        # Crash-rerun overwrite-not-append: an identical v1 draft already recorded (from a
        # turn re-executed after a hard kill) is returned as-is, so the re-run neither rewrites
        # the file nor mirrors a duplicate cloud output asset.
        v1_path = self._artifact_dir(owner_id, project_id, artifact_id) / "v1"
        v1 = self._load_json(v1_path, None)
        if v1 is not None and v1.get("content") == content:
            return {
                "artifact_id": v1["artifact_id"],
                "project_id": project_id,
                "version": v1["version"],
                "status": v1["status"],
                "generated_by_execution": v1.get("generated_by_execution"),
                "created_by": v1.get("created_by"),
                "idempotent": True,
            }
        # Producer invariant (docs/research/04 §5): agent output carries a non-null
        # generated_by_execution; user intake carries a non-null created_by and a null
        # generated_by_execution.
        record = {
            "artifact_id": artifact_id,
            "project_id": project_id,
            "owner_id": str(owner_id),
            "name": artifact_id,
            "version": 1,
            "status": "DRAFT",
            "content": content,
            "idempotency_key": idempotency_key,
            "generated_by_execution": generated_by_execution,
            "created_by": str(owner_id),
            "cloud_output_asset_id": None,
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
        }
        # Project the draft into the task's cloud outputs/ (best-effort; scratch is the
        # authority). The created cloud asset id is persisted with the record so a later
        # version/promote updates it in place instead of exploding assets.
        await self._mirror_output(owner_id, project, record, content)
        self._save_json(
            self._artifact_dir(owner_id, project_id, artifact_id) / "v1", record
        )
        return {
            "artifact_id": artifact_id,
            "project_id": project_id,
            "version": 1,
            "status": "DRAFT",
            "generated_by_execution": generated_by_execution,
            "created_by": str(owner_id),
            "idempotent": False,
        }

    async def promote_to_drive(
        self,
        owner_id: uuid.UUID,
        project_id: str,
        *,
        artifact_id: str,
        promote_idempotency_key: str | None = None,
    ) -> dict:
        """Promote the latest artifact version to the Drive + RAG queue.

        Idempotent: a record already marked ``PROMOTED`` is returned as ``idempotent=True``
        without touching the Drive. ``promote_idempotency_key`` is recorded for auditability
        (the router derives it as ``research:{project_id}:{artifact_id}:{version}``).

        Two promote shapes:
        - **Cloud task folder** (created via ``create_task``): the artifact is already
          projected into the task folder's cloud ``outputs/<id>.md``; promotion just flips
          that asset to RAG pending (no upload, no asset explosion).
        - **Skill project** (created via ``research_project``): the report is uploaded to
          ``research/<project_id>/`` and mirrored into a scratch ``outputs/`` projection.
        """
        project = self._load_project(owner_id, project_id)
        version_paths = sorted(
            (self._artifact_dir(owner_id, project_id, artifact_id)).glob("v*"),
            key=lambda p: int(p.name[1:]),
        )
        if not version_paths:
            raise ValueError(f"artifact has no scratch content: {artifact_id}")
        record = self._load_json(version_paths[-1], None)
        if record is None:
            raise ValueError(f"artifact not found: {artifact_id}")
        if record.get("status") == "PROMOTED" and record.get("drive_asset_id"):
            return self._promoted_view(record, idempotent=True)
        if project.get("cloud_folder_id"):
            # Task path: the report lives in the cloud outputs/ projection already.
            if not record.get("cloud_output_asset_id"):
                await self._mirror_output(owner_id, project, record, record["content"])
            cloud_asset_id = record["cloud_output_asset_id"]
            await self.drive.mark_rag_pending(uuid.UUID(cloud_asset_id))
            record["status"] = "PROMOTED"
            record["drive_asset_id"] = cloud_asset_id
            record["drive_path"] = (
                f"{project['cloud_folder_path']}/outputs/{_safe_filename(artifact_id)}.md"
            )
            record["rag_status"] = "PENDING"
            record["promote_idempotency_key"] = promote_idempotency_key
            record["updated_at"] = _now_iso()
            self._save_json(version_paths[-1], record)
            return self._promoted_view(record, idempotent=False)
        asset = await self.drive.save_artifact(
            owner_id,
            name=f"{artifact_id}.md",
            mime_type="text/markdown",
            content=record["content"].encode("utf-8"),
            folder_path=f"research/{project_id}",
        )
        await self.drive.mark_rag_pending(asset.id)
        record["status"] = "PROMOTED"
        record["drive_asset_id"] = str(asset.id)
        record["drive_path"] = f"research/{project_id}/{asset.name}"
        record["rag_status"] = "PENDING"
        record["promote_idempotency_key"] = promote_idempotency_key
        record["updated_at"] = _now_iso()
        self._save_json(version_paths[-1], record)
        # Derived projection: mirror the promoted report into outputs/ so the task
        # folder carries a stable, tenant-scoped copy of what went to the drive + RAG.
        # (artifacts/ is the epistemic authority; outputs/ is a projection.)
        outputs_dir = self._project_dir(owner_id, project_id) / "outputs"
        outputs_dir.mkdir(parents=True, exist_ok=True)
        (outputs_dir / f"{_safe_filename(artifact_id)}.md").write_text(
            record["content"], encoding="utf-8"
        )
        return self._promoted_view(record, idempotent=False)

    @staticmethod
    def _promoted_view(record: dict, *, idempotent: bool) -> dict:
        return {
            "artifact_id": record["artifact_id"],
            "project_id": record["project_id"],
            "version": record["version"],
            "status": record["status"],
            "drive_asset_id": record.get("drive_asset_id"),
            "drive_path": record.get("drive_path"),
            "rag_status": record.get("rag_status"),
            "promote_idempotency_key": record.get("promote_idempotency_key"),
            "idempotent": idempotent,
        }

    def read_artifact(
        self, owner_id: uuid.UUID, project_id: str, *, artifact_id: str, version: int | None = None
    ) -> dict:
        version = version or 1
        record = self._artifact(owner_id, project_id, artifact_id, version)
        return {
            "artifact_id": artifact_id,
            "project_id": project_id,
            "version": record["version"],
            "status": record["status"],
            "content": record["content"],
        }

    async def create_version(
        self,
        owner_id: uuid.UUID,
        project_id: str,
        *,
        artifact_id: str,
        content: str,
        idempotency_key: str | None = None,
    ) -> dict:
        if idempotency_key:
            existing = self._find_by_idempotency(owner_id, "artifact", idempotency_key)
            if existing is not None:
                return {
                    "artifact_id": existing["artifact_id"],
                    "project_id": project_id,
                    "version": existing["version"],
                    "idempotent": True,
                }
        project = self._load_project(owner_id, project_id)
        artifact_dir = self._artifact_dir(owner_id, project_id, artifact_id)
        next_version = 1
        if artifact_dir.is_dir():
            next_version = max((int(p.name[1:]) for p in artifact_dir.glob("v*")), default=0) + 1
        # Crash-rerun overwrite-not-append: replaying an identical latest version returns it
        # instead of minting a new one (a hard kill mid-turn can re-run the same produce
        # call with byte-identical content). First-write / genuinely-new-content behaviour is
        # unchanged.
        if next_version > 1:
            latest = self._load_json(artifact_dir / f"v{next_version - 1}", None)
            if latest is not None and latest.get("content") == content:
                return {
                    "artifact_id": artifact_id,
                    "project_id": project_id,
                    "version": latest["version"],
                    "idempotent": True,
                }
        previous = self._artifact(owner_id, project_id, artifact_id, next_version - 1)
        record = dict(previous)
        # ``cloud_output_asset_id`` is intentionally carried over from the previous version so
        # the cloud outputs/<id>.md projection is updated in place, never re-created.
        record.update(
            {
                "artifact_id": artifact_id,
                "version": next_version,
                "status": "DRAFT",
                "content": content,
                "idempotency_key": idempotency_key,
                "drive_asset_id": None,
                "drive_path": None,
                "rag_status": None,
                "created_at": _now_iso(),
                "updated_at": _now_iso(),
            }
        )
        await self._mirror_output(owner_id, project, record, content)
        self._save_json(artifact_dir / f"v{next_version}", record)
        return {
            "artifact_id": artifact_id,
            "project_id": project_id,
            "version": next_version,
            "idempotent": False,
        }

    def diff_artifact(
        self,
        owner_id: uuid.UUID,
        project_id: str,
        *,
        artifact_id: str,
        from_version: int,
        to_version: int,
    ) -> dict:
        a = self._artifact(owner_id, project_id, artifact_id, from_version)
        b = self._artifact(owner_id, project_id, artifact_id, to_version)
        changed = [k for k in ("content", "status", "drive_path") if a.get(k) != b.get(k)]
        return {"artifact_id": artifact_id, "from": from_version, "to": to_version, "changed": changed}

    # ── chat-driven tasks (the console read-only surface) ──────────────────
    # A "research task" is a project folder created atomically from the chat (+ Research
    # button) rather than from an inbox. Everything below is tenant-scoped by owner;
    # task/asset ids are resolved through ``_resolve_owned_path`` so a ``..``/absolute
    # segment cannot escape the owner root.

    @staticmethod
    def _task_view(project: dict) -> dict:
        return {
            "task_id": project["id"],
            "name": project["name"],
            "owner_id": project["owner_id"],
            "stage": project["stage"],
            "status": project["status"],
            "gates": project["gates"],
            "session_id": project.get("session_id"),
            "cloud_folder_id": project.get("cloud_folder_id"),
            "cloud_folder_path": project.get("cloud_folder_path"),
            "deletion_requested": project.get("deletion_requested", False),
            "is_running": project.get("active_run") is not None,
            "created_at": project.get("created_at"),
            "updated_at": project.get("updated_at"),
            # The most recent terminal run outcome (finished/blocked/stalled/cancelled/error);
            # the desktop renders it as a status-panel banner until the next run starts.
            "last_block": project.get("last_block"),
        }

    def list_tasks(self, owner_id: uuid.UUID) -> list[dict]:
        """Every task under the owner's scratch root (skips non-task dirs)."""
        root = self._owner_root(owner_id)
        if not root.is_dir():
            return []
        tasks: dict[str, dict] = {}
        for task_dir in root.iterdir():
            if not task_dir.is_dir():
                continue
            project = self._load_json(task_dir / "project.json", None)
            if project is None:
                continue
            view = self._task_view(project)
            # One scratch dir per task, so duplicates shouldn't exist — but a crash mid-create
            # could leave a stray dir claiming a task_id. Dedup by task_id so the monitor
            # never renders the same task twice (the frontend clears its container, but the
            # list source itself must be unique).
            tasks.setdefault(view["task_id"], view)
        return sorted(tasks.values(), key=lambda t: t["updated_at"] or "", reverse=True)

    async def create_task(
        self,
        owner_id: uuid.UUID,
        *,
        title: str,
        description: str = "",
        parent_folder_path: str = "",
        material_asset_ids: list[str] | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        """Atomically create a research task folder + its cloud-drive projection, in one call.

        One request does everything: a cloud task folder under ``parent_folder_path`` (My
        Drive) named after the title, the authoritative scratch state (``project.json`` born
        in DISCOVER / ACTIVE, ``graph.json``, ``task_spec.json``, ``session_history.json``),
        mirrors of the two JSON files into the cloud folder, and each material copied into the
        cloud ``materials/`` folder (provenance recorded in ``project.json["materials"]``).
        A failing material copy rolls the whole thing back — cloud folder soft-deleted into
        Trash, scratch removed — so a rejected create leaves no half-built task.
        """
        if idempotency_key:
            existing = self._find_by_idempotency(owner_id, "project", idempotency_key)
            if existing is not None:
                return {
                    **self._task_view(existing),
                    "idempotent": True,
                    "materials": existing.get("materials", []),
                }
        task_id = str(uuid.uuid4())
        # The task folder (entity) is a cloud folder: user-visible under the chosen working
        # directory. ``cloud_folder_id`` is the authoritative binding; ``cloud_folder_path``
        # is the display cache (create_folder auto-suffixes busy names).
        cloud_folder = await self.drive.create_folder(
            owner_id,
            None,  # My Drive (personal scope)
            parent_folder_path.strip() or None,
            _safe_filename(title.strip()) or f"task-{task_id[:8]}",
        )
        cloud_folder_id = cloud_folder["id"]
        project = {
            "id": task_id,
            "owner_id": str(owner_id),
            "name": title.strip(),
            "profile": "research_task",
            "status": "ACTIVE",
            "stage": "DISCOVER",
            "gates": {gate: "NOT_RUN" for gate in _GATES},
            "idempotency_key": idempotency_key,
            "cloud_folder_id": cloud_folder_id,
            "cloud_folder_path": cloud_folder["path"],
            "materials": [],
            "cloud_mirrors": {},
            "project_revision": 0,      # monotonic version; bumped by every atomic commit
            "driver": None,             # driver checkpoint (materialized on first run)
            "last_block": None,         # terminal-stop record {kind, reason, at, execution_id}
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
        }
        try:
            # The user-visible task folder always carries the two work folders, even before
            # any material is copied or artifact promoted, so the working-directory layout is
            # stable from the moment the task exists (a task with no materials/outputs yet
            # still shows both folders in the drive).
            await self.drive.create_folder(owner_id, None, cloud_folder["path"], "materials")
            await self.drive.create_folder(owner_id, None, cloud_folder["path"], "outputs")
            self._save_project(project)
            self._save_graph(owner_id, task_id, {"nodes": [], "edges": []})
            task_spec = {
                "title": title.strip(),
                "description": (description or "").strip(),
                "created_at": _now_iso(),
                "created_by": str(owner_id),
            }
            session_history = {"session_id": None, "turns": []}
            self._save_json(self._project_dir(owner_id, task_id) / "task_spec.json", task_spec)
            self._save_json(
                self._project_dir(owner_id, task_id) / "session_history.json", session_history
            )
            await self._mirror_cloud(
                owner_id, project, "task_spec.json", json.dumps(task_spec, ensure_ascii=False, indent=2)
            )
            await self._mirror_cloud(
                owner_id,
                project,
                "session_history.json",
                json.dumps(session_history, ensure_ascii=False, indent=2),
            )
            copied: list[dict] = []
            if material_asset_ids:
                for asset_id in material_asset_ids:
                    copied.append(await self._copy_material(owner_id, project, asset_id))
            materials = copied

            def mutate(project: dict) -> None:
                project["materials"] = materials

            self.atomic_update_project(owner_id, task_id, mutate)
        except Exception:
            # Roll back atomically: soft-delete the cloud task folder and hard-delete the
            # scratch state. A rejected create never leaves a half-materialized task behind
            # (the router surfaces the underlying DriveError).
            try:
                await self.drive.delete_folder(owner_id, uuid.UUID(cloud_folder_id))
            except Exception as exc:  # noqa: BLE001 - best-effort cleanup, never mask the create
                logger.debug("research create rollback: folder delete failed: %s", exc)
            shutil.rmtree(self._project_dir(owner_id, task_id), ignore_errors=True)
            raise
        return {**self._task_view(project), "idempotent": False, "materials": copied}

    async def _copy_material(self, owner_id: uuid.UUID, project: dict, asset_id: str) -> dict:
        """Copy one cloud-drive asset into the task's cloud ``materials/`` folder.

        ``drive.download`` enforces the ownership/visibility gate (``ensure_asset_readable``
        → 403/404 for a cross-tenant asset), so a user can never smuggle another user's file
        into a task folder. The copy lands in My Drive (``workspace_id=None``) under the task
        folder's ``materials/`` subfolder, named ``<asset_id>__<safe_name>`` so two assets
        sharing a display name never overwrite each other. Returns the provenance row recorded
        in ``project.json["materials"]``.
        """
        mime, name, data = await self.drive.download(owner_id, uuid.UUID(asset_id))
        if data is None:
            raise DriveError("material bytes missing", 404)
        cloud_asset = await self.drive.save_artifact(
            owner_id,
            name=f"{asset_id}__{_safe_filename(name)}",
            mime_type=mime,
            content=data,
            folder_path=f"{project['cloud_folder_path']}/materials",
            workspace_id=None,
        )
        return {
            "asset_id": asset_id,
            "name": name,
            "cloud_asset_id": str(cloud_asset.id),
            "mime": mime,
        }

    async def _mirror_cloud(self, owner_id: uuid.UUID, project: dict, name: str, content: str) -> bool:
        """Mirror one text file into the task's cloud folder (create once, update in place).

        Best-effort: scratch / the DB remain authoritative, so a cloud failure only logs and
        never fails the caller's authoritative write. Returns ``True`` when a new cloud asset
        was created — the caller should then persist the updated ``project["cloud_mirrors"]``.
        """
        cloud_folder_path = project.get("cloud_folder_path")
        if not cloud_folder_path:
            return False
        mirrors = project.setdefault("cloud_mirrors", {})
        asset_id = mirrors.get(name)
        try:
            if asset_id:
                await self.drive.update_content(owner_id, uuid.UUID(asset_id), content)
                return False
            asset = await self.drive.save_artifact(
                owner_id,
                name=name,
                mime_type="application/json" if name.endswith(".json") else "text/markdown",
                content=content.encode("utf-8"),
                folder_path=cloud_folder_path,
                workspace_id=None,
            )
            mirrors[name] = str(asset.id)
            return True
        except Exception:
            logger.exception("research cloud mirror failed for %s", name)
            return False

    async def _mirror_output(self, owner_id: uuid.UUID, project: dict, record: dict, content: str) -> bool:
        """Project one artifact into the task's cloud ``outputs/<artifact_id>.md``.

        Best-effort like :meth:`_mirror_cloud`: scratch ``artifacts/`` is the authority. The
        created asset id is stored on ``record["cloud_output_asset_id"]`` so a later version
        or promote updates it in place. Returns ``True`` when a new cloud asset was created.
        """
        cloud_folder_path = project.get("cloud_folder_path")
        if not cloud_folder_path:
            return False
        asset_id = record.get("cloud_output_asset_id")
        try:
            if asset_id:
                await self.drive.update_content(owner_id, uuid.UUID(asset_id), content)
                return False
            asset = await self.drive.save_artifact(
                owner_id,
                name=f"{_safe_filename(record['artifact_id'])}.md",
                mime_type="text/markdown",
                content=content.encode("utf-8"),
                folder_path=f"{cloud_folder_path}/outputs",
                workspace_id=None,
            )
            record["cloud_output_asset_id"] = str(asset.id)
            return True
        except Exception:
            logger.exception("research cloud output mirror failed for %s", record["artifact_id"])
            return False

    # ── session <-> task binding (one session, one task) ────────────────────
    # The owner-level ``_session_index.json`` maps session_id -> task_id. It is a routing
    # index only: the authoritative conversation data stays in the DB ``SessionModel``, and
    # ``session_history.json`` is the task-local mirror for the console.

    def _session_index_path(self, owner_id: uuid.UUID) -> Path:
        return self._resolve_owned_path(owner_id, "_session_index.json")

    def _load_session_index(self, owner_id: uuid.UUID) -> dict:
        return self._load_json(self._session_index_path(owner_id), {})

    def _save_session_index(self, owner_id: uuid.UUID, index: dict) -> None:
        self._save_json(self._session_index_path(owner_id), index)

    def bind_session(self, owner_id: uuid.UUID, task_id: str, session_id) -> dict:
        """Bind a chat session to a task (idempotent for the same pair).

        One session may drive one task only: binding an already-bound session to a different
        task raises ``ValueError`` (→ Conflict) so chat turns can never fan out across tasks.
        """
        self._load_project(owner_id, task_id)  # raises if the task is missing
        index = self._load_session_index(owner_id)
        prev = index.get(str(session_id))
        if prev is not None and prev != task_id:
            raise ValueError(f"session already bound to a different research task: {prev}")
        index[str(session_id)] = task_id
        self._save_session_index(owner_id, index)

        def mutate(project: dict) -> None:
            project["session_id"] = str(session_id)

        self.atomic_update_project(owner_id, task_id, mutate)
        mirror = self._project_dir(owner_id, task_id) / "session_history.json"
        data = self._load_json(mirror, {"session_id": None, "turns": []})
        data["session_id"] = str(session_id)
        self._save_json(mirror, data)
        return {"task_id": task_id, "session_id": str(session_id)}

    def task_id_for_session(self, owner_id: uuid.UUID, session_id) -> str | None:
        return self._load_session_index(owner_id).get(str(session_id))

    def bound_session_ids(self, owner_id: uuid.UUID) -> set[str]:
        """Every chat session id bound to one of this owner's research tasks.

        Research sessions are a different kind from normal chats (one per task, driven from
        the Research monitor), so the chat sidebar hides them; this is the filter set.
        """
        return set(self._load_session_index(owner_id).keys())

    async def append_session_turn(
        self, owner_id: uuid.UUID, session_id, role: str, content: str, ts: str | None = None
    ) -> None:
        """Mirror one chat turn into the bound task's scratch + cloud ``session_history.json``.

        Best-effort and non-authoritative: the DB ``SessionModel`` remains the source of
        truth. A session that is not bound to any task is a no-op.
        """
        task_id = self.task_id_for_session(owner_id, session_id)
        if task_id is None:
            return
        mirror = self._project_dir(owner_id, task_id) / "session_history.json"
        data = self._load_json(mirror, {"session_id": str(session_id), "turns": []})
        data.setdefault("turns", []).append(
            {"role": role, "content": content, "ts": ts or _now_iso()}
        )
        self._save_json(mirror, data)
        project = self._load_project(owner_id, task_id)
        created = await self._mirror_cloud(
            owner_id,
            project,
            "session_history.json",
            json.dumps(data, ensure_ascii=False, indent=2),
        )
        if created:
            # Persist the new mirror binding atomically (best-effort mirror path).
            mirrors = project.get("cloud_mirrors") or {}

            def mutate(project: dict) -> None:
                project.setdefault("cloud_mirrors", {}).update(mirrors)

            self.atomic_update_project(owner_id, task_id, mutate)

    # ── task status / artifact reads ────────────────────────────────────────
    def list_artifacts(self, owner_id: uuid.UUID, task_id: str) -> list[dict]:
        """Latest version of every artifact in the task, newest first."""
        artifacts_dir = self._resolve_owned_path(owner_id, task_id, "artifacts")
        if not artifacts_dir.is_dir():
            return []
        artifacts: list[dict] = []
        for artifact_dir in artifacts_dir.iterdir():
            if not artifact_dir.is_dir():
                continue
            versions = sorted(
                (p for p in artifact_dir.glob("v*") if p.name[1:].isdigit()),
                key=lambda p: int(p.name[1:]),
            )
            if not versions:
                continue
            record = self._load_json(versions[-1], None)
            if record is None:
                continue
            artifacts.append(
                {
                    "artifact_id": artifact_dir.name,
                    "task_id": task_id,
                    "version": record["version"],
                    "status": record["status"],
                    "drive_asset_id": record.get("drive_asset_id"),
                    "drive_path": record.get("drive_path"),
                    "rag_status": record.get("rag_status"),
                    "updated_at": record.get("updated_at"),
                }
            )
        return sorted(artifacts, key=lambda a: a["updated_at"] or "", reverse=True)

    async def get_task_status(self, owner_id: uuid.UUID, task_id: str) -> dict:
        """Task + gates + grouped graph nodes + artifacts + material/output listings.

        Task state and artifacts come from scratch (the authority); materials/outputs are the
        user-visible cloud projection (listed from the task folder in the drive). The monitor
        is low-frequency, so a per-call ``list_files`` is acceptable.
        """
        project = self._load_project(owner_id, task_id)
        graph = self._load_graph(owner_id, task_id)
        nodes_by_type: dict[str, list[dict]] = {}
        for node in graph["nodes"]:
            nodes_by_type.setdefault(node["type"], []).append(node)
        cloud_path = project.get("cloud_folder_path")
        if cloud_path:
            files = await self.drive.list_files(owner_id)
            materials = sorted(
                a["name"] for a in files if a["folder_path"] == f"{cloud_path}/materials"
            )
            outputs = sorted(
                a["name"] for a in files if a["folder_path"] == f"{cloud_path}/outputs"
            )
            # The full working-directory projection: every file inside the task's cloud folder
            # (root mirrors task_spec.json / session_history.json + materials/ + outputs/), so
            # the monitor can show exactly what the task folder holds.
            cloud_files = [
                {
                    "id": a["id"],
                    "name": a["name"],
                    "folder_path": a["folder_path"] or "",
                    "mime_type": a["mime_type"],
                    "size": a["size"],
                    "rag_status": a["rag_status"],
                    "updated_at": a["updated_at"],
                }
                for a in files
                if (a["folder_path"] or "") == cloud_path
                or (a["folder_path"] or "").startswith(f"{cloud_path}/")
            ]
        else:
            materials, outputs, cloud_files = [], [], []
        return {
            **self._task_view(project),
            # Domain-authoritative monotonic version: every atomic commit bumps it; the
            # monitor/SSE layer uses it to order snapshots and drop out-of-order events.
            "project_revision": project.get("project_revision", 0),
            "description": self._load_json(
                self._project_dir(owner_id, task_id) / "task_spec.json", {}
            ).get("description", ""),
            "graph": graph,
            "nodes": nodes_by_type,
            "artifacts": self.list_artifacts(owner_id, task_id),
            "materials": materials,
            "outputs": outputs,
            "cloud_files": cloud_files,
            # The bound research session's conversation (task-local mirror of the chat that
            # drives this task; the DB SessionModel stays the authoritative chat record).
            "session": self._load_json(
                self._project_dir(owner_id, task_id) / "session_history.json",
                {"session_id": None, "turns": []},
            ),
            # Gate overrides awaiting a human decision — surfaced to the desktop so the user
            # can Approve / Reject them as an interactive card in the task's chat.
            "pending_overrides": [
                {
                    "approval_id": a["id"],
                    "gate_name": a.get("gate_name"),
                    # Agent's free-form reason is secondary context only: trimmed for display so
                    # the card never renders an unbounded LLM blob (docs/research/10 §6).
                    "reason": _trim_reason(a.get("reason", "")),
                }
                for a in self.pending_overrides(owner_id, task_id)
            ],
        }

    async def delete_task(self, owner_id: uuid.UUID, task_id: str) -> dict:
        """Delete a research task: 409-guarded, cloud folder soft-deleted, scratch removed.

        Two P0 guards block deletion: a task with a RUNNING execution (mutex with the agent)
        and a report the knowledge base already indexed (remove it from RAG first). The
        request is recorded in ``project.json`` *before* teardown so a crash mid-delete is
        auditable. The cloud folder goes to Trash (soft delete); restoring it does NOT
        resurrect the task — scratch is hard-deleted, so the task state is gone.
        """
        project = self._load_project(owner_id, task_id)  # 404 if missing / traversal

        # P0: never delete while the agent is mid-execution. Both the higher-level server-owned
        # run slot (``active_run``) and any per-tool RUNNING execution block deletion; the slot
        # is the authority for a live server-side run even when no execution record is mid-flight.
        if project.get("active_run") and project["active_run"].get("status") == "RUNNING":
            raise ValueError("Research task is currently running")
        executions = self._load_json(
            self._project_dir(owner_id, task_id) / "executions.json", {"executions": []}
        )
        if any(e.get("status") == "RUNNING" for e in executions["executions"]):
            raise ValueError("Research task is currently running")

        # P0: never delete a report the knowledge base already indexed. Check both the scratch
        # artifact record (promote snapshot) and the cloud outputs asset (the worker's truth).
        artifacts_dir = self._resolve_owned_path(owner_id, task_id, "artifacts")
        if artifacts_dir.is_dir():
            for artifact_dir in artifacts_dir.iterdir():
                if not artifact_dir.is_dir():
                    continue
                versions = sorted(
                    (p for p in artifact_dir.glob("v*") if p.name[1:].isdigit()),
                    key=lambda p: int(p.name[1:]),
                )
                if not versions:
                    continue
                record = self._load_json(versions[-1], None)
                if record and record.get("rag_status") == "INDEXED":
                    raise ValueError("Please remove from Knowledge Base first")
        if project.get("cloud_folder_path"):
            cloud_path = project["cloud_folder_path"]
            for f in await self.drive.list_files(owner_id):
                if f["folder_path"] == f"{cloud_path}/outputs" and f["rag_status"] == "INDEXED":
                    raise ValueError("Please remove from Knowledge Base first")

        # Mark the deletion request before teardown so an interrupted delete is auditable.
        def mutate(project: dict) -> None:
            project["deletion_requested"] = True

        self.atomic_update_project(owner_id, task_id, mutate)

        # Cloud folder: soft-delete the whole subtree into Trash (files + folder rows). If it
        # is already gone from the drive, scratch cleanup below is still the source of truth.
        if project.get("cloud_folder_id"):
            try:
                await self.drive.delete_folder(owner_id, uuid.UUID(project["cloud_folder_id"]))
            except DriveError:
                pass

        # Scratch is runtime state: clear the session routing index, then hard-delete the
        # task directory. Restoring the Trash folder cannot resurrect the task.
        index = self._load_session_index(owner_id)
        cleaned = {k: v for k, v in index.items() if v != task_id}
        if len(cleaned) != len(index):
            self._save_session_index(owner_id, cleaned)
        shutil.rmtree(self._project_dir(owner_id, task_id), ignore_errors=True)
        return {"deleted": True}

    # ── research_state ────────────────────────────────────────────────────
    def get_state(self, owner_id: uuid.UUID, project_id: str) -> dict:
        project = self._load_project(owner_id, project_id)
        return {
            "project_id": project["id"],
            "stage": project["stage"],
            "status": project["status"],
            "gates": project["gates"],
        }

    def transition_stage(
        self, owner_id: uuid.UUID, project_id: str, *, target: str
    ) -> dict:
        project = self._load_project(owner_id, project_id)
        current = project["stage"]
        if target not in _STAGES:
            return {
                "requested": target,
                "granted": False,
                "stage": current,
                "reason": f"unknown stage {target!r}",
            }
        if _LEGAL_NEXT.get(current) != target:
            return {
                "requested": target,
                "granted": False,
                "stage": current,
                "reason": f"illegal transition {current} -> {target}",
            }
        gate = _GATE_BEFORE.get(target)
        if gate and project["gates"].get(gate) not in ("PASS", "OVERRIDE"):
            return {
                "requested": target,
                "granted": False,
                "stage": current,
                "reason": f"transition {current} -> {target} guarded by {gate}: "
                f"not passed (current {project['gates'].get(gate)})",
            }

        def mutate(project: dict) -> None:
            project["stage"] = target

        project = self.atomic_update_project(owner_id, project_id, mutate)
        return {"requested": target, "granted": True, "stage": project["stage"], "gate": gate}

    def get_handoff(self, owner_id: uuid.UUID, project_id: str) -> dict:
        project = self._load_project(owner_id, project_id)
        return {
            "project_id": project["id"],
            "stage": project["stage"],
            "next_stage": _LEGAL_NEXT.get(project["stage"]),
            "gate_required": _GATE_BEFORE.get(_LEGAL_NEXT.get(project["stage"]) or ""),
        }

    # ── research_evidence ─────────────────────────────────────────────────
    def record_node(
        self, owner_id: uuid.UUID, project_id: str, *, node: dict
    ) -> dict:
        """Append a graph node, idempotent by ``node.id``.

        A crash-rerun that replays an already-recorded node returns the existing node instead
        of raising (the driver re-executes a turn after a hard kill and must not duplicate
        graph writes). First-write behavior is unchanged; the extra ``idempotent`` flag tells
        the caller whether it created the node or found it.
        """
        graph = self._load_graph(owner_id, project_id)
        # The node's ``id``/``type`` are the graph's identity keys — index them only after a
        # precise guard. A bare ``KeyError: 'id'`` (a probe node like ``{type, title}``) tells
        # the model nothing about what is missing, so it cannot correct the call.
        if not isinstance(node, dict):
            raise TypeError("record_node 'node' must be an object, not a string")
        if "id" not in node or "type" not in node:
            raise ValueError(
                "record_node 'node' must include 'id' and 'type' "
                f"(got keys: {sorted(node)})"
            )
        node_id = node["id"]
        node_type = node["type"]
        existing = next((n for n in graph["nodes"] if n["id"] == node_id), None)
        if existing is not None:
            return {"node": existing, "idempotent": True}
        record = {
            "id": node_id,
            "type": node_type,
            "label": node.get("label", node_id),
            "status": node.get("status", "VALID"),
            **{k: v for k, v in node.items() if k not in ("id", "type", "label", "status")},
        }
        graph["nodes"].append(record)
        self._save_graph(owner_id, project_id, graph)
        return {"node": record, "idempotent": False}

    def link_edge(
        self, owner_id: uuid.UUID, project_id: str, *, src: str, dst: str, kind: str
    ) -> dict:
        """Link two recorded nodes, deduplicated by ``(src, dst, kind)``.

        Re-linking an existing ``(src, dst, kind)`` tuple is a no-op that returns the existing
        edge (crash reruns must not fan out duplicate dependency edges).
        """
        graph = self._load_graph(owner_id, project_id)
        ids = {n["id"] for n in graph["nodes"]}
        if src not in ids or dst not in ids:
            raise ValueError(f"edge endpoints must be recorded nodes: {src} -> {dst}")
        edge = {"src": src, "dst": dst, "kind": kind}
        existing = next(
            (e for e in graph["edges"] if e["src"] == src and e["dst"] == dst and e["kind"] == kind),
            None,
        )
        if existing is not None:
            return {"edge": existing, "idempotent": True}
        graph["edges"].append(edge)
        self._save_graph(owner_id, project_id, graph)
        return {"edge": edge, "idempotent": False}

    def _cascade(
        self, graph: dict, start_id: str, *, to_invalid: bool
    ) -> list[str]:
        """BFS over dependency edges: mark the downstream closure STALE (or INVALID)."""
        nodes = {n["id"]: n for n in graph["nodes"]}
        affected: list[str] = []
        seen = {start_id}
        queue = [start_id]
        while queue:
            cur = queue.pop(0)
            for edge in graph["edges"]:
                neighbor = None
                if edge["src"] == cur and edge["kind"] in _FORWARD_DEPS:
                    neighbor = edge["dst"]
                elif edge["dst"] == cur and edge["kind"] in _REVERSE_DEPS:
                    neighbor = edge["src"]
                elif edge["src"] == cur and edge["kind"] in _INVALIDATES:
                    neighbor = edge["dst"]
                if neighbor is None or neighbor in seen or neighbor not in nodes:
                    continue
                seen.add(neighbor)
                nodes[neighbor]["status"] = "INVALID" if to_invalid else "STALE"
                affected.append(neighbor)
                queue.append(neighbor)
        return affected

    def mutate_node(
        self, owner_id: uuid.UUID, project_id: str, *, node_id: str, patch: dict
    ) -> dict:
        graph = self._load_graph(owner_id, project_id)
        target = next((n for n in graph["nodes"] if n["id"] == node_id), None)
        if target is None:
            raise ValueError(f"node not found: {node_id}")
        to_invalid = "status" in patch and patch["status"] == "INVALID"
        protected = {"id", "type"}
        for key, value in patch.items():
            if key not in protected:
                target[key] = value
        cascade = self._cascade(graph, node_id, to_invalid=to_invalid)
        self._save_graph(owner_id, project_id, graph)
        return {"node": target, "cascade": cascade}

    def invalidate_downstream(
        self, owner_id: uuid.UUID, project_id: str, *, node_id: str
    ) -> dict:
        graph = self._load_graph(owner_id, project_id)
        target = next((n for n in graph["nodes"] if n["id"] == node_id), None)
        if target is None:
            raise ValueError(f"node not found: {node_id}")
        target["status"] = "INVALID"
        cascade = self._cascade(graph, node_id, to_invalid=True)
        self._save_graph(owner_id, project_id, graph)
        return {"node": target, "cascade": cascade}

    def query_lineage(
        self, owner_id: uuid.UUID, project_id: str, *, node_id: str
    ) -> dict:
        graph = self._load_graph(owner_id, project_id)
        nodes = {n["id"]: n for n in graph["nodes"]}
        if node_id not in nodes:
            raise ValueError(f"node not found: {node_id}")

        def dependents(start: str) -> list[str]:
            """Nodes affected when ``start`` changes (the STALE/INVALID cascade closure)."""
            out: list[str] = []
            seen = {start}
            queue = [start]
            while queue:
                cur = queue.pop(0)
                for edge in graph["edges"]:
                    nxt = None
                    if edge["src"] == cur and edge["kind"] in _FORWARD_DEPS:
                        nxt = edge["dst"]
                    elif edge["dst"] == cur and edge["kind"] in _REVERSE_DEPS:
                        nxt = edge["src"]
                    elif edge["src"] == cur and edge["kind"] in _INVALIDATES:
                        nxt = edge["dst"]
                    if nxt is not None and nxt not in seen:
                        seen.add(nxt)
                        out.append(nxt)
                        queue.append(nxt)
            return sorted(out)

        def dependencies(start: str) -> list[str]:
            """Nodes ``start`` depends on (the reverse closure)."""
            out: list[str] = []
            seen = {start}
            queue = [start]
            while queue:
                cur = queue.pop(0)
                for edge in graph["edges"]:
                    nxt = None
                    if edge["dst"] == cur and edge["kind"] in _FORWARD_DEPS:
                        nxt = edge["src"]
                    elif edge["src"] == cur and edge["kind"] in _REVERSE_DEPS:
                        nxt = edge["dst"]
                    elif edge["dst"] == cur and edge["kind"] in _INVALIDATES:
                        nxt = edge["src"]
                    if nxt is not None and nxt not in seen:
                        seen.add(nxt)
                        out.append(nxt)
                        queue.append(nxt)
            return sorted(out)

        return {
            "node": nodes[node_id],
            "ancestors": dependencies(node_id),
            "descendants": dependents(node_id),
        }

    # ── research_gate ─────────────────────────────────────────────────────
    def _evidence_checks(self, project_id: str, graph: dict) -> list[dict]:
        nodes = {n["id"]: n for n in graph["nodes"]}
        sources = [n for n in graph["nodes"] if n["type"] == "Source"]
        evidences = [n for n in graph["nodes"] if n["type"] == "Evidence"]
        claims = [n for n in graph["nodes"] if n["type"] == "Claim"]

        sources_ok = bool(sources) and all(
            s.get("verification_status") == _VERIFIED for s in sources
        )
        edges = graph["edges"]

        def linked_to_verified(evidence_id: str) -> bool:
            for edge in edges:
                if edge["src"] == evidence_id or edge["dst"] == evidence_id:
                    other = edge["dst"] if edge["src"] == evidence_id else edge["src"]
                    src_node = nodes.get(other)
                    if (
                        src_node
                        and src_node["type"] == "Source"
                        and src_node.get("verification_status") == _VERIFIED
                    ):
                        return True
            return False

        def linked_to_evidence(claim_id: str) -> bool:
            return any(
                edge["src"] == claim_id or edge["dst"] == claim_id
                for edge in edges
                if (edge["src"] in nodes and nodes[edge["src"]]["type"] == "Evidence")
                or (edge["dst"] in nodes and nodes[edge["dst"]]["type"] == "Evidence")
            )

        upstream_invalid = any(
            n["status"] == "INVALID"
            for n in graph["nodes"]
            if n["type"] in {"Source", "Evidence", "Claim", "Result"}
        )
        linked_evidence_ok = bool(evidences) and all(
            linked_to_verified(e["id"]) for e in evidences
        )
        linked_claims_ok = bool(claims) and all(
            linked_to_evidence(c["id"]) for c in claims
        )
        verified_count = sum(
            1 for s in sources if s.get("verification_status") == _VERIFIED
        )
        return [
            {
                "name": "sources_verified",
                "ok": sources_ok,
                "detail": (
                    "at least one Source exists and every Source is verified"
                    if sources_ok
                    else (
                        "need at least one Source node with verification_status='verified'"
                        if not sources
                        else (
                            "every Source must be verification_status='verified': "
                            f"{verified_count}/{len(sources)} verified"
                        )
                    )
                ),
            },
            {
                "name": "evidence_linked",
                "ok": linked_evidence_ok,
                "detail": (
                    "every Evidence node links to a verified Source"
                    if linked_evidence_ok
                    else "every Evidence node must link to a verified Source"
                ),
            },
            {
                "name": "no_invalid_upstream",
                "ok": not upstream_invalid,
                "detail": "no INVALID upstream evidence/claim/source",
            },
            {
                "name": "claim_draft_links",
                "ok": linked_claims_ok,
                "detail": (
                    "every Claim links to an Evidence node"
                    if linked_claims_ok
                    else (
                        "need at least one Claim linked to an Evidence node"
                        if not claims
                        else (
                            f"{len(claims) - sum(1 for c in claims if linked_to_evidence(c['id']))}/"
                            f"{len(claims)} Claims lack an Evidence link; every Claim must link to an Evidence node"
                        )
                    )
                ),
            },
        ]

    def check_gate(
        self, owner_id: uuid.UUID, project_id: str, *, gate_name: str
    ) -> dict:
        if gate_name not in _GATES:
            raise ValueError(f"unknown gate: {gate_name}")
        project = self._load_project(owner_id, project_id)
        status = project["gates"].get(gate_name, "NOT_RUN")
        if status == "OVERRIDE":
            return {"status": "OVERRIDE", "gate_name": gate_name, "checks": []}
        graph = self._load_graph(owner_id, project_id)
        if gate_name == "EVIDENCE_GATE":
            checks = self._evidence_checks(project_id, graph)
        elif gate_name == "DESIGN_GATE":
            checks = self._design_checks(graph)
        elif gate_name == "CLAIM_GATE":
            checks = self._claim_checks(graph)
        else:  # QUALITY_GATE
            checks = self._quality_checks(project)
        ok = all(c["ok"] for c in checks)
        status = "PASS" if ok else "FAIL"

        def mutate(project: dict) -> None:
            project["gates"][gate_name] = status

        self.atomic_update_project(owner_id, project_id, mutate)
        return {"status": status, "gate_name": gate_name, "checks": checks}

    @staticmethod
    def _design_checks(graph: dict) -> list[dict]:
        designs = [n for n in graph["nodes"] if n["type"] == "Design"]
        d = designs[0] if designs else {}
        fields = [f for f in ("register", "estimand", "identification", "risk") if d.get(f)]
        return [
            {
                "name": "design_fields",
                "ok": bool(designs) and len(fields) == 4,
                "detail": "Design node carries register/estimand/identification/risk",
            }
        ]

    @staticmethod
    def _claim_checks(graph: dict) -> list[dict]:
        claims = [n for n in graph["nodes"] if n["type"] == "Claim"]
        ok = bool(claims) and all(
            c.get("citations") and c.get("strength") in _ALLOWED_CLAIM_STRENGTH for c in claims
        )
        return [
            {
                "name": "claims_anchored",
                "ok": ok,
                "detail": "every Claim has citations and an allowed strength",
            }
        ]

    @staticmethod
    def _quality_checks(project: dict) -> list[dict]:
        scorecard = project.get("scorecard") or []
        ok = len(scorecard) >= 7 and not any(row.get("fatal") for row in scorecard)
        return [
            {
                "name": "scorecard",
                "ok": ok,
                "detail": "scorecard has >=7 rows and no fatal finding",
            }
        ]

    def explain_failure(
        self, owner_id: uuid.UUID, project_id: str, *, gate_name: str
    ) -> dict:
        result = self.check_gate(owner_id, project_id, gate_name=gate_name)
        failed = [c for c in result.get("checks", []) if not c["ok"]]
        return {"gate_name": gate_name, "status": result["status"], "failed_checks": failed}

    def request_override(
        self, owner_id: uuid.UUID, project_id: str, *, gate_name: str, reason: str
    ) -> dict:
        if gate_name not in _GATES:
            raise ValueError(f"unknown gate: {gate_name}")
        self._load_project(owner_id, project_id)  # existence check (ValueError when missing)
        approval = {
            "id": str(uuid.uuid4()),
            "project_id": project_id,
            "gate_name": gate_name,
            "reason": reason,
            "status": "PENDING",
            # PENDING invariant (docs/research/02 §2.8 + 11): null until a human resolves.
            "approver_user_id": None,
            "resolved_at": None,
            "requester_agent": "research_gate",
            "created_at": _now_iso(),
        }
        approvals = self._load_json(
            self._project_dir(owner_id, project_id) / "approvals.json", {"approvals": []}
        )
        approvals["approvals"].append(approval)
        self._save_json(self._project_dir(owner_id, project_id) / "approvals.json", approvals)
        return {
            "approval_id": approval["id"],
            "gate_name": gate_name,
            "status": "PENDING",
            "approver_user_id": None,
            "resolved_at": None,
        }

    def resolve_override(
        self,
        owner_id: uuid.UUID,
        approval_id: str,
        *,
        approve: bool,
        project_id: str | None = None,
    ) -> dict:
        # Locate the approval across the owner's projects (or restrict to one project when the
        # caller — the human-approval API — already resolved the task id from the URL).
        approval = None
        project = None
        if project_id is not None:
            dirs = [self._project_dir(owner_id, project_id)]
        else:
            owner_dir = self._owner_root(owner_id)
            dirs = list(owner_dir.iterdir()) if owner_dir.is_dir() else []
        for project_dir in dirs:
            if not project_dir.is_dir():
                continue
            approvals = self._load_json(project_dir / "approvals.json", {"approvals": []})
            for a in approvals["approvals"]:
                if a["id"] == approval_id:
                    approval = a
                    project = self._load_json(project_dir / "project.json", None)
                    break
            if approval is not None:
                break
        if approval is None:
            raise ValueError(f"approval not found: {approval_id}")
        if approval["status"] != "PENDING":
            raise ValueError(f"approval already resolved: {approval['status']}")
        approval["status"] = "APPROVED" if approve else "REJECTED"
        approval["approver_user_id"] = str(owner_id)
        approval["resolved_at"] = _now_iso()
        approvals = self._load_json(
            self._project_dir(owner_id, project["id"]) / "approvals.json", {"approvals": []}
        )
        for a in approvals["approvals"]:
            if a["id"] == approval_id:
                a.update(approval)
        self._save_json(self._project_dir(owner_id, project["id"]) / "approvals.json", approvals)
        if approve and project is not None:
            def mutate(project: dict) -> None:
                project["gates"][approval["gate_name"]] = "OVERRIDE"

            self.atomic_update_project(owner_id, project["id"], mutate)
        return {
            "approval_id": approval_id,
            "status": approval["status"],
            "gate_name": approval["gate_name"],
            "approver_user_id": str(owner_id),
            "resolved_at": approval["resolved_at"],
        }

    # ── gate review notes (auto chat explanation) ─────────────────────────
    def _gate_checks_readonly(
        self, owner_id: uuid.UUID, project_id: str, gate_name: str
    ) -> list[dict]:
        """Run a gate's mechanical checks WITHOUT recording a verdict.

        ``check_gate`` writes ``gates[gate] = PASS/FAIL`` into ``project.json`` (a side
        effect that must never happen while merely explaining a gate). This path reuses the
        same pure check functions but is strictly read-only.
        """
        if gate_name == "EVIDENCE_GATE":
            return self._evidence_checks(
                project_id, self._load_graph(owner_id, project_id)
            )
        if gate_name == "DESIGN_GATE":
            return self._design_checks(self._load_graph(owner_id, project_id))
        if gate_name == "CLAIM_GATE":
            return self._claim_checks(self._load_graph(owner_id, project_id))
        if gate_name == "QUALITY_GATE":
            return self._quality_checks(self._load_project(owner_id, project_id))
        raise ValueError(f"unknown gate: {gate_name}")

    def gate_note_drafts(self, owner_id: uuid.UUID, project_id: str) -> list[dict]:
        """Draft one ``system`` note per unnoted PENDING gate approval.

        Read-only: never mutates gate state, ``approvals.json``, or the ``_GATE_NOTE_KEY``
        marker. The caller must persist each ``{approval_id, text}`` to the session DB first
        and only then call :meth:`mark_gate_notes` (DB write always precedes the marker).
        """
        project = self._load_project(owner_id, project_id)  # existence check (ValueError)
        noted = set(project.get(_GATE_NOTE_KEY) or [])
        drafts: list[dict] = []
        for approval in self.pending_overrides(owner_id, project_id):
            approval_id = approval.get("id")
            if approval_id in noted:
                continue
            gate_name = approval.get("gate_name", "")
            if gate_name not in _GATES:
                continue
            checks = self._gate_checks_readonly(owner_id, project_id, gate_name)
            failed = [c for c in checks if not c.get("ok")]
            text = compose_gate_review_note(gate_name, failed, approval.get("reason") or "")
            if text:
                drafts.append({"approval_id": approval_id, "text": text})
        return drafts

    def mark_gate_notes(
        self, owner_id: uuid.UUID, project_id: str, approval_ids: list[str]
    ) -> None:
        """Record that ``system`` notes were durably written for ``approval_ids`` (CAS).

        Callers invoke this ONLY after the corresponding notes committed to the session DB —
        the marker must never be consumed ahead of a durable write (a DB failure leaves the
        marker untouched so a later attempt can retry). Only approvals still PENDING and not
        already noted are added, so one approval id yields at most one note; a fresh approval
        (e.g. after a human Reject) gets its own id and its own note.
        """
        if not approval_ids:
            return
        approvals = self._load_json(
            self._project_dir(owner_id, project_id) / "approvals.json", {"approvals": []}
        )
        pending_ids = {
            a["id"] for a in approvals["approvals"] if a.get("status") == "PENDING"
        }
        to_add = set(approval_ids) & pending_ids
        if not to_add:
            return

        def mutate(project: dict) -> None:
            noted = set(project.get(_GATE_NOTE_KEY) or [])
            project[_GATE_NOTE_KEY] = sorted(noted | to_add)

        self.atomic_update_project(owner_id, project_id, mutate)

    async def emit_gate_notes(
        self,
        session_factory,
        owner_id: uuid.UUID,
        project_id: str,
        session_id: str | None,
    ) -> int:
        """Durably write the gate review notes for a parked run, then mark them.

        Ordering contract: each ``system`` note is committed to the session DB *before* its
        ``approval_id`` is added to the marker (never marker-first). On a DB failure the id
        is left unmarked so a later call can retry. Callers must invoke this BEFORE the run's
        ``blocked`` wake-up is published, so a monitor refetch observes the note.
        """
        if not session_id:
            return 0
        drafts = self.gate_note_drafts(owner_id, project_id)
        if not drafts:
            return 0
        from core.infrastructure.memory import insert_plain_message  # local: API/worker bridge

        written = 0
        for draft in drafts:
            last_exc: Exception | None = None
            for attempt in range(_DB_INSERT_ATTEMPTS):
                try:
                    await insert_plain_message(
                        session_factory,
                        owner_id,
                        uuid.UUID(session_id),
                        "system",
                        draft["text"],
                    )
                    break
                except Exception as exc:  # noqa: BLE001 - retried, then surfaced as a warning
                    last_exc = exc
                    if attempt + 1 < _DB_INSERT_ATTEMPTS:
                        await asyncio.sleep(_DB_INSERT_RETRY_DELAY * (attempt + 1))
            else:
                # All attempts failed: leave the marker unconsumed so a later retry can write it.
                logger.warning(
                    "research gate note insert failed for approval %s (not marked): %s",
                    draft["approval_id"],
                    last_exc,
                )
                continue
            # DB committed -> only now consume the marker for this approval id.
            self.mark_gate_notes(owner_id, project_id, [draft["approval_id"]])
            written += 1
            try:
                await self.append_session_turn(
                    owner_id, session_id, "system", draft["text"]
                )
            except Exception as exc:  # noqa: BLE001 - mirror is advisory, never fatal
                logger.debug("research gate note mirror failed for %s: %s", project_id, exc)
        return written

    # ── research_run ──────────────────────────────────────────────────────
    def begin_run(
        self,
        owner_id: uuid.UUID,
        project_id: str,
        *,
        session_id: str | None = None,
        stale_after_seconds: int = 7200,
    ) -> dict:
        """Acquire the single active-run slot for a task (mutex over concurrent turns).

        One task may hold at most one live run at a time (T4 invariant #2: concurrent
        Task A/Task B runs are fine, but a single task never runs two turns in parallel).
        A RUNNING slot older than ``stale_after_seconds`` is presumed dead — the owning
        process crashed before ``end_run`` — and is adopted: any RUNNING executions it
        left behind are flipped to ABORTED so the delete guard never blocks forever.

        Raises ``ValueError`` for a live conflict; the router maps it to a 409.
        """
        run_id = str(uuid.uuid4())

        def mutate(project: dict) -> None:
            active = project.get("active_run")
            if active and active.get("status") == "RUNNING":
                started = active.get("started_at")
                # ``_now_iso`` is fixed-width UTC, so lexicographic comparison is chronological.
                cutoff = time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ",
                    time.gmtime(time.time() - stale_after_seconds),
                )
                stale = not started or started < cutoff
                if not stale:
                    raise ValueError("Research task is already running")
                logger.warning("research begin_run adopted stale active_run: %s", active)
                # The dead process's RUNNING executions are stale too — unblock the delete guard.
                exec_path = self._project_dir(owner_id, project_id) / "executions.json"
                data = self._load_json(exec_path, {"executions": []})
                changed = False
                for execution in data["executions"]:
                    if execution.get("status") == "RUNNING":
                        execution["status"] = "ABORTED"
                        execution["result"] = {"aborted": True, "reason": "stale run adopted"}
                        execution["finished_at"] = _now_iso()
                        changed = True
                if changed:
                    self._save_json(exec_path, data)
            project["active_run"] = {
                "run_id": run_id,
                "session_id": session_id,
                "started_at": _now_iso(),
                "status": "RUNNING",
            }
            # Reset the driver ledger for this run: a new run = a new run_id, and the old
            # run's turn/cost/no-progress state must never leak into it.
            project["driver"] = {
                **self._empty_driver(),
                "run_id": run_id,
                "turn_state": "done",
                "started_at": _now_iso(),
                "updated_at": _now_iso(),
            }

        project = self.atomic_update_project(owner_id, project_id, mutate)
        return dict(project["active_run"])

    def end_run(self, owner_id: uuid.UUID, project_id: str) -> dict:
        """Release the active-run slot (idempotent: a missing slot is a no-op)."""
        holder: dict[str, Any] = {}

        def mutate(project: dict) -> None:
            active = project.pop("active_run", None)
            holder["run_id"] = active["run_id"] if active else None

        self.atomic_update_project(owner_id, project_id, mutate)
        return {"run_id": holder.get("run_id"), "status": "IDLE"}

    def record_execution(
        self,
        owner_id: uuid.UUID,
        project_id: str,
        *,
        tool: str,
        args: dict,
        execution_id: str | None = None,
    ) -> dict:
        """Append one tool-execution audit row, idempotent by ``execution_id``.

        A crash rerun can hand a deterministic ``execution_id`` (e.g. the driver's
        ``run_id:turn_index:turn_attempt``) so re-recording the same execution is a no-op
        instead of a second RUNNING row. Without it the behaviour is unchanged: a fresh uuid
        and one appended row.
        """
        self._load_project(owner_id, project_id)
        path = self._project_dir(owner_id, project_id) / "executions.json"
        data = self._load_json(path, {"executions": []})
        if execution_id is not None:
            existing = next(
                (e for e in data["executions"] if e["execution_id"] == execution_id), None
            )
            if existing is not None:
                return {
                    "execution_id": existing["execution_id"],
                    "status": existing["status"],
                    "idempotent": True,
                }
        execution = {
            "execution_id": execution_id or str(uuid.uuid4()),
            "project_id": project_id,
            "tool": tool,
            "args": args,
            "status": "RUNNING",
            "result": None,
            "created_at": _now_iso(),
        }
        data["executions"].append(execution)
        self._save_json(path, data)
        return {"execution_id": execution["execution_id"], "status": "RUNNING", "idempotent": False}

    def finish_execution(
        self, owner_id: uuid.UUID, project_id: str, *, execution_id: str, result: Any
    ) -> dict:
        path = self._project_dir(owner_id, project_id) / "executions.json"
        data = self._load_json(path, {"executions": []})
        execution = next(
            (e for e in data["executions"] if e["execution_id"] == execution_id), None
        )
        if execution is None:
            raise ValueError(f"execution not found: {execution_id}")
        if execution["status"] == "SUCCESS":
            raise ValueError("execution is immutable: already finished")
        execution["status"] = "SUCCESS"
        execution["result"] = result
        execution["finished_at"] = _now_iso()
        self._save_json(path, data)
        return {"execution_id": execution_id, "status": "SUCCESS"}

    def execute_sandbox_script(
        self, owner_id: uuid.UUID, project_id: str, *, script: str
    ) -> dict:
        # Spike stand-in: the profile gate decides whether code may run. The literature MVP
        # profile blocks the sandbox; a real Phase 1 dispatches the script to the sandbox.
        project = self._load_project(owner_id, project_id)
        allowed = project.get("profile") in ("empirical", "mixed")
        execution = self.record_execution(
            owner_id, project_id, tool="research_run.execute_sandbox_script", args={"script": script}
        )
        return {
            **self.finish_execution(
                owner_id,
                project_id,
                execution_id=execution["execution_id"],
                result={"blocked": not allowed, "reason": "sandbox profile-gated" if not allowed else "ok"},
            ),
            "blocked": not allowed,
        }


# ── tool plumbing ─────────────────────────────────────────────────────────────
def _render_json(args: dict, value: Any) -> list:
    return [text_block(json.dumps(value, ensure_ascii=False, indent=2, default=str))]


def _make_tool(
    *,
    name: str,
    description: str,
    parameters: dict,
    handler: Any,
    permission: set[ToolPermission],
) -> Any:
    return define_tool(
        name=name,
        description=description,
        parameters=parameters,
        output=ToolOutput(schema={"type": "object"}, render=_render_json),
        execute=handler,
        is_concurrency_safe=True,
        permission=permission,
    )


_COMMON_OBJ = {
    "project_id": {"type": "string", "description": "Research project id."},
    "artifact_id": {"type": "string", "description": "Research artifact id."},
    "node_id": {"type": "string", "description": "Graph node id."},
    "gate_name": {
        "type": "string",
        "enum": _GATES,
        "description": "Gate to check / override.",
    },
    "idempotency_key": {
        "type": "string",
        "description": "Replay key: same key returns the identical record without re-doing work.",
    },
    "content": {"type": "string", "description": "Markdown artifact content."},
    "version": {"type": "integer", "description": "Artifact version."},
}


def _params(actions: list[str], extra: dict, required: list[str]) -> dict:
    """Build a research tool's argument schema with ``action`` pinned to its valid set.

    ``action`` is the discriminator that selects the handler branch, so it must be an
    **enum** of the tool's real action names — a free-form string lets a weaker tool-calling
    model invent verbs like ``list`` / ``query`` / ``read`` that no handler implements, which
    froze every research_evidence / research_artifact call on the worker. The enum mirrors the
    handler's own ``if action == ...`` branches exactly, so it never excludes a working path.
    """
    props = {
        "action": {
            "type": "string",
            "enum": actions,
            "description": "Which action to run on this tool. One of: "
            + ", ".join(actions),
        },
        **_COMMON_OBJ,
        **extra,
    }
    return {"type": "object", "properties": props, "required": ["action", *required]}


def build_research_plugin(ctx: Any | None = None) -> Plugin:
    """Build the research plugin, capturing ``ctx`` for lazy capability resolution.

    Tools do **not** resolve ``drive``/``research_scratch`` at build time — they call
    ``_service_for(ctx)`` at execute time, so the plugin can be registered (and stay a PENDING
    fiber) before the API provides its capabilities. This mirrors the toolkit factory pattern
    and keeps ``discover()`` compatibility (no module-level ``PLUGIN``).
    """
    from plugins.research.monitor import MUTATING_ACTIONS

    def service() -> ResearchService:
        if ctx is None:
            raise RuntimeError("research plugin was built without a Context")
        return ResearchService(
            drive=ctx.resolve("drive"),
            scratch_root=ctx.resolve("research_scratch"),
        )

    def user() -> uuid.UUID:
        return _current_user()

    def _handoff_project_id() -> str | None:
        """The project id sunk into this turn's context, or ``None``.

        The desktop "Start deep research" button resumes a Research OS project via a
        structured handoff (``ChatRequest.handoff``); the API sinks it into
        ``current_turn().context`` so a ``resume`` tool call still targets the right
        project even if the model omits ``project_id`` from its args.
        """
        turn = current_turn()
        if turn is None or not turn.context:
            return None
        handoff = turn.context.get("handoff") or {}
        if handoff.get("kind") != "research":
            return None
        return handoff.get("project_id")

    def _project_id(args: dict, tool: str, action: str) -> str:
        """The acting project id: the explicit ``project_id`` arg, else the bound handoff.

        The worker threads ``current_turn().context["handoff"].project_id`` (the task id) on
        every auto turn and the desktop resume sinks the same handoff, so a call that omits
        ``project_id`` still targets the bound project instead of dying with a ``KeyError``.
        """
        project_id = args.get("project_id") or _handoff_project_id()
        if not project_id:
            raise ValueError(
                f"{tool} {action} needs a project_id: pass it in the tool call or run inside "
                "a bound research handoff"
            )
        return project_id

    def _require(args: dict, key: str, tool: str, action: str) -> Any:
        """A required argument with a precise, actionable error (never a bare ``KeyError``).

        The model sees the tool, action, and the missing key so it can correct the call
        instead of guessing at a ``KeyError`` repr (``'node_id'`` etc.) and burning turns.
        """
        if key not in args or args[key] is None:
            raise ValueError(f"{tool} {action} is missing required argument '{key}'")
        return args[key]

    def _monitor_wrap(tool_name: str, handler):
        """Post-success publish hook for a research tool router.

        After a *mutating* action returns, emit a revision wake-up so the desktop monitor
        refetches the task snapshot. Best-effort: a publish failure (no Redis bus, no owner
        context, transient lock error) must never turn a successful tool call into an error.
        """

        async def _wrapped(args: dict, exec: ToolExecution) -> dict:
            result = await handler(args, exec)
            action = args.get("action")
            if action in MUTATING_ACTIONS.get(tool_name, set()):
                try:
                    project_id = args.get("project_id") or _handoff_project_id()
                    if project_id:
                        await service().publish_change(user(), project_id, kind=action)
                except Exception:
                    logger.debug("research monitor publish skipped (best-effort)", exc_info=True)
            return result

        return _wrapped

    async def _project(args: dict, exec: ToolExecution) -> dict:
        svc = service()
        action = args["action"]
        if action == "create":
            return await svc.create_project(
                user(),
                name=args["name"],
                profile=args.get("profile", "literature"),
                idempotency_key=args.get("idempotency_key"),
            )
        if action in ("resume", "snapshot", "archive"):
            project_id = args.get("project_id") or _handoff_project_id()
            if not project_id:
                raise ValueError(f"research_project {action} requires project_id")
            if action == "resume":
                return svc.resume_project(user(), project_id)
            if action == "snapshot":
                return svc.snapshot_project(user(), project_id)
            return svc.archive_project(user(), project_id)
        raise ValueError(f"unknown research_project action: {action}")

    async def _artifact(args: dict, exec: ToolExecution) -> dict:
        svc = service()
        action = args["action"]
        if action == "write_scratch":
            return await svc.write_scratch(
                user(), _project_id(args, "research_artifact", action),
                artifact_id=_require(args, "artifact_id", "research_artifact", action),
                content=_require(args, "content", "research_artifact", action),
                idempotency_key=args.get("idempotency_key"),
                generated_by_execution=args.get("generated_by_execution"),
            )
        if action == "promote_to_drive":
            return await svc.promote_to_drive(
                user(), _project_id(args, "research_artifact", action),
                artifact_id=_require(args, "artifact_id", "research_artifact", action),
            )
        if action == "read":
            return svc.read_artifact(
                user(), _project_id(args, "research_artifact", action),
                artifact_id=_require(args, "artifact_id", "research_artifact", action),
                version=args.get("version"),
            )
        if action == "create_version":
            return await svc.create_version(
                user(), _project_id(args, "research_artifact", action),
                artifact_id=_require(args, "artifact_id", "research_artifact", action),
                content=_require(args, "content", "research_artifact", action),
                idempotency_key=args.get("idempotency_key"),
            )
        if action == "diff":
            return svc.diff_artifact(
                user(), _project_id(args, "research_artifact", action),
                artifact_id=_require(args, "artifact_id", "research_artifact", action),
                from_version=_require(args, "from_version", "research_artifact", action),
                to_version=_require(args, "to_version", "research_artifact", action),
            )
        raise ValueError(f"unknown research_artifact action: {action}")

    async def _state(args: dict, exec: ToolExecution) -> dict:
        svc = service()
        action = args["action"]
        if action == "get_state":
            return svc.get_state(user(), _project_id(args, "research_state", action))
        if action == "transition_stage":
            return svc.transition_stage(
                user(), _project_id(args, "research_state", action),
                target=_require(args, "target", "research_state", action),
            )
        if action == "get_handoff":
            return svc.get_handoff(user(), _project_id(args, "research_state", action))
        raise ValueError(f"unknown research_state action: {action}")

    async def _evidence(args: dict, exec: ToolExecution) -> dict:
        svc = service()
        action = args["action"]
        if action == "record_node":
            return svc.record_node(
                user(), _project_id(args, "research_evidence", action),
                node=_require(args, "node", "research_evidence", action),
            )
        if action == "link_edge":
            return svc.link_edge(
                user(), _project_id(args, "research_evidence", action),
                src=_require(args, "src", "research_evidence", action),
                dst=_require(args, "dst", "research_evidence", action),
                kind=_require(args, "kind", "research_evidence", action),
            )
        if action == "query_lineage":
            return svc.query_lineage(
                user(), _project_id(args, "research_evidence", action),
                node_id=_require(args, "node_id", "research_evidence", action),
            )
        if action == "mutate_node":
            return svc.mutate_node(
                user(), _project_id(args, "research_evidence", action),
                node_id=_require(args, "node_id", "research_evidence", action),
                patch=_require(args, "patch", "research_evidence", action),
            )
        if action == "invalidate_downstream":
            return svc.invalidate_downstream(
                user(), _project_id(args, "research_evidence", action),
                node_id=_require(args, "node_id", "research_evidence", action),
            )
        raise ValueError(f"unknown research_evidence action: {action}")

    async def _gate(args: dict, exec: ToolExecution) -> dict:
        svc = service()
        action = args["action"]
        if action == "check":
            return svc.check_gate(
                user(), _project_id(args, "research_gate", action),
                gate_name=_require(args, "gate_name", "research_gate", action),
            )
        if action == "explain_failure":
            return svc.explain_failure(
                user(), _project_id(args, "research_gate", action),
                gate_name=_require(args, "gate_name", "research_gate", action),
            )
        if action == "request_override":
            return svc.request_override(
                user(), _project_id(args, "research_gate", action),
                gate_name=_require(args, "gate_name", "research_gate", action),
                reason=args.get("reason") or "",
            )
        if action == "resolve_override":
            return svc.resolve_override(
                user(), _require(args, "approval_id", "research_gate", action),
                approve=args.get("approve", True),
            )
        raise ValueError(f"unknown research_gate action: {action}")

    async def _run(args: dict, exec: ToolExecution) -> dict:
        svc = service()
        action = args["action"]
        if action == "record_execution":
            return svc.record_execution(
                user(), _project_id(args, "research_run", action),
                tool=_require(args, "tool", "research_run", action),
                args=args.get("args", {}),
            )
        if action == "finish_execution":
            return svc.finish_execution(
                user(), _project_id(args, "research_run", action),
                execution_id=_require(args, "execution_id", "research_run", action),
                result=args.get("result"),
            )
        if action == "execute_sandbox_script":
            return svc.execute_sandbox_script(
                user(), _project_id(args, "research_run", action),
                script=_require(args, "script", "research_run", action),
            )
        raise ValueError(f"unknown research_run action: {action}")

    research_project_tool = _make_tool(
        name="research_project",
        description=(
            "Create / resume / snapshot / archive a ResearchProject. Projects are the "
            "tenant-scoped container for a research OS workflow (state machine in "
            "docs/research/07)."
        ),
        parameters=_params(
            ["create", "resume", "snapshot", "archive"],
            {
                "name": {"type": "string", "description": "Project display name."},
                "profile": {
                    "type": "string",
                    "description": "Research profile (Method x Output), e.g. literature.",
                },
            },
            required=[],
        ),
        handler=_monitor_wrap("research_project", _project),
        permission={ToolPermission.READ, ToolPermission.WRITE},
    )

    research_artifact_tool = _make_tool(
        name="research_artifact",
        description=(
            "Write scratch content, promote to the cloud drive (folder research/<project_id>), "
            "version, read, and diff a ResearchArtifact. Promotion marks the drive asset "
            "RAG_PENDING, triggering the RAG projection worker."
        ),
        parameters=_params(
            ["write_scratch", "promote_to_drive", "read", "create_version", "diff"],
            {
                "generated_by_execution": {
                    "type": "string",
                    "description": "execution_id when the content is agent-generated.",
                },
                "from_version": {"type": "integer", "description": "diff base version."},
                "to_version": {"type": "integer", "description": "diff target version."},
            },
            required=[],
        ),
        handler=_monitor_wrap("research_artifact", _artifact),
        permission={ToolPermission.READ, ToolPermission.WRITE},
    )

    research_state_tool = _make_tool(
        name="research_state",
        description=(
            "Read the project state machine, request a stage transition, and inspect the "
            "handoff. Transitions are legal-transition-only: the state machine rejects "
            "illegal jumps and un-passed gate guards."
        ),
        parameters=_params(
            ["get_state", "transition_stage", "get_handoff"],
            {"target": {"type": "string", "description": "Requested next stage."}},
            required=[],
        ),
        handler=_monitor_wrap("research_state", _state),
        permission={ToolPermission.READ},
    )

    research_evidence_tool = _make_tool(
        name="research_evidence",
        description=(
            "Record graph nodes and edges, query lineage, and invalidate downstream nodes. "
            "Mutating an upstream node STALE-cascades to its epistemic dependents."
        ),
        parameters=_params(
            ["record_node", "link_edge", "query_lineage", "mutate_node", "invalidate_downstream"],
            {
                "node": {
                    "type": "object",
                    "description": "Node record: {id, type, label?, status?, ...}",
                },
                "patch": {
                    "type": "object",
                    "description": "Node mutation fields (e.g. {status: 'INVALID'}).",
                },
                "src": {"type": "string", "description": "Edge source node id."},
                "dst": {"type": "string", "description": "Edge destination node id."},
                "kind": {
                    "type": "string",
                    "description": "Edge kind: derived_from / generated_by / depends_on / "
                    "supports / cites / tests / invalidates / ...",
                },
            },
            required=[],
        ),
        handler=_monitor_wrap("research_evidence", _evidence),
        permission={ToolPermission.READ, ToolPermission.WRITE},
    )

    research_gate_tool = _make_tool(
        name="research_gate",
        description=(
            "Check a gate, explain a failure, or request/resolve a human override. Checks are "
            "deterministic; an override always spawns a PENDING ResearchApproval that only a "
            "human can resolve (never self-approve)."
        ),
        parameters=_params(
            ["check", "explain_failure", "request_override", "resolve_override"],
            {
                "reason": {"type": "string", "description": "Override justification (human review)."},
                "approval_id": {"type": "string", "description": "Approval id to resolve."},
                "approve": {
                    "type": "boolean",
                    "description": "Human verdict: true approves, false rejects.",
                },
            },
            required=[],
        ),
        handler=_monitor_wrap("research_gate", _gate),
        permission={ToolPermission.READ, ToolPermission.WRITE},
    )

    research_run_tool = _make_tool(
        name="research_run",
        description=(
            "Record (and finish) an immutable ResearchExecution audit row, or execute a "
            "sandbox script. Sandbox execution is profile-gated; the literature profile blocks it."
        ),
        parameters=_params(
            ["record_execution", "finish_execution", "execute_sandbox_script"],
            {
                "tool": {"type": "string", "description": "Tool name being audited."},
                "args": {"type": "object", "description": "Execution arguments."},
                "execution_id": {"type": "string", "description": "Execution id to finish."},
                "result": {"type": "object", "description": "Execution result payload."},
                "script": {"type": "string", "description": "Sandbox script body."},
            },
            required=[],
        ),
        handler=_monitor_wrap("research_run", _run),
        permission={ToolPermission.READ, ToolPermission.WRITE},
    )

    return Plugin(
        name="research",
        description="Research OS: 6 tools over the ResearchProject/Artifact/Graph/State/Gate/Execution contracts.",
        tools=[
            research_project_tool,
            research_artifact_tool,
            research_state_tool,
            research_evidence_tool,
            research_gate_tool,
            research_run_tool,
        ],
        inject=["drive", "research_scratch"],
    )


def register_research_plugins(manager, ctx: Any | None = None) -> None:
    """Mount the research plugin on ``manager`` (used by ``apps/api/deps.py``)."""
    manager.register(build_research_plugin(ctx))
