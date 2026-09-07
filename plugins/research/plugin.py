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
import hashlib
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

from workflow.ledger import finish_into as ledger_finish_into
from workflow.ledger import record_into as ledger_record_into

from plugins.research.workflow_spec import RESEARCH_WORKFLOW

logger = logging.getLogger(__name__)

from agent.engine.context import current_turn
from agent.engine.decisions import ToolExecution, text_block
from agent.plugins.base import Plugin
from agent.tools.definition import ToolOutput, define_tool
from agent.tools.tool_permissions import ToolPermission
from core.application.drive_service import DriveError
from core.infrastructure.request_context import get_request_user_id
from core.infrastructure.web_fetch import (
    DEFAULT_TEXT_TARGET,
    MIN_FETCH_TEXT_CHARS,
    fetch_clean_urls,
)
from core.infrastructure.web_fetch import (
    canonical_url as canonicalize,
)
from plugins.research.batch import (
    compute_pending,
    evidence_fingerprint,
    gate_ok as _batch_gate_ok,
)
from plugins.research.fetch_cache import (
    FetchStore,
    FlightRegistry,
    cache_index_id,
    classify_eligibility,
)

# Fetch transport/resolver overrides — production runs with the real network (None); unit
# tests patch these globals to a MockTransport + stub resolver so nothing is hit offline.
_FETCH_TRANSPORT_OVERRIDE = None
_FETCH_RESOLVER_OVERRIDE = None

# P3-2B: process-local single-flight over the *network* fetch, keyed by the full P3-2A
# cache identity (canonical_url × policy × parser). One asyncio event loop only — the
# registry holds futures; there is deliberately no cross-process/thread dedup tier.
_FETCH_FLIGHTS = FlightRegistry()

# Source/Evidence node id prefixes (deterministic, url-anchored — E1/E5 identity keys).
_SOURCE_ID_PREFIX = "src:"
_EVIDENCE_ID_PREFIX = "ev:"
_FETCH_PROVENANCE_KEY = "_fetch_provenance"

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

# Report-style confidence words the deep_research skill asks for in prose
# ("high/medium/low") normalized onto the canonical Claim strengths. The gate vocabulary
# and the writing vocabulary disagreed, which made CLAIM_GATE mechanically unsatisfiable.
_CLAIM_STRENGTH_ALIASES = {"high": "confident", "medium": "supported", "low": "asserted"}


def _claim_strength_error(value: Any) -> ValueError:
    return ValueError(
        f"claim strength {value!r} is not valid; use one of "
        f"{sorted(_ALLOWED_CLAIM_STRENGTH)} (report-style 'high'/'medium'/'low' are "
        "accepted and normalized)"
    )


def normalize_claim_strength(value: Any) -> str | None:
    """Canonicalize one Claim ``strength``; ``None`` = not a recognizable strength.

    Write-side (record_node / mutate_node) rejects ``None`` at the boundary with a repair
    hint; read-side (``_claim_checks``) applies the same mapping so graphs recorded before
    this normalization (raw ``high/medium``) still score valid.
    """
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    if v in _ALLOWED_CLAIM_STRENGTH:
        return v
    return _CLAIM_STRENGTH_ALIASES.get(v)


def _coerce_json_container(value: Any, *, expect: str) -> tuple[Any, bool]:
    """P3-3 protocol repair: unwrap a container argument the model JSON-encoded as a string.

    A known LLM failure mode is double-serialization — passing ``"[{...}]"`` where the
    contract asks for the array itself. This helper performs the *one* deterministic repair
    (``json.loads``) at the existing validation boundary and then hands the value straight
    back into the unchanged checks, so a repaired call is 100% equivalent to a native one.

    Zero guessing (P3-3 constraint 3): the parsed value must match ``expect``
    (``"list"`` or ``"dict"``) EXACTLY; unparseable text, scalars, or the wrong container
    are returned untouched so every existing reject path fires verbatim. Returns
    ``(value, coerced)``; ``coerced`` is the audit flag — the raw payload is never logged.
    """
    if not isinstance(value, str):
        return value, False
    try:
        parsed = json.loads(value)
    except ValueError:
        return value, False
    ok = parsed.__class__ is list if expect == "list" else parsed.__class__ is dict
    return (parsed, True) if ok else (value, False)


def _with_coerced(result: dict, coerced: bool, name: str) -> dict:
    """Attach the P3-3 audit marker (``"coerced": ["<name>_from_json_string"]``) to a
    result dict ONLY when a repair actually happened — native calls stay byte-identical."""
    if coerced:
        result["coerced"] = [f"{name}_from_json_string"]
    return result

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


# ── run progress events (scratch log the worker drains into the bound session chat) ──
# ``run_events.json`` is append-only per task. A run's driver checkpoint keeps a
# ``progress_cursor`` (last event seq already surfaced); the worker drains only events newer
# than the cursor, one ``system`` row each (role=system rows are filtered from model context
# everywhere). Exactly four event kinds keep the chat flood-free and semantically
# idempotent (a (run_seq, type, key) triple is appended at most once):
#   stage           - a stage transition was granted
#   gate_diagnostic - progressive mode recorded a failing gate AND advanced (distinct event
#                     from the stage change, so the two are never collapsed)
#   artifact        - a genuinely new artifact / new artifact version was produced
#   terminal        - the run reached a graded terminal outcome (worker-appended)
# Ordinary overwrite updates, micro-retries and individual tool steps are NEVER events.
_RUN_EVENTS_FILE = "run_events.json"
_RUN_EVENT_TYPES = ("stage", "gate_diagnostic", "artifact", "terminal")
_SCRAPE_SUBDIR = "scrape"


def _slug_token(token: str, limit: int = 40) -> str:
    """A filesystem-safe slug for a user-supplied query/fragment (anti-explosion).

    ``_safe_filename`` strips path/control characters; this additionally collapses spaces and
    underscores and truncates so a long natural-language query stays one short filename part.
    """
    cleaned = _safe_filename(token or "").replace(" ", "_")
    cleaned = re.sub(r"_{2,}", "_", cleaned).strip("_")
    return (cleaned or "query")[:limit].strip("_")


def _report_stem(artifact_id: str) -> str:
    """The report's base filename without a trailing ``.md``, for versioned output names.

    ``draft.md`` -> ``draft`` so the versioned final lands at ``outputs/draft_v1.md`` (a raw
    ``draft.md_v1.md`` would be ugly and is why the suffix is stripped first).
    """
    stem = _safe_filename(artifact_id)
    if stem.lower().endswith(".md"):
        stem = stem[:-3]
    return stem.rstrip(".")


def _is_report_artifact(artifact_id: str) -> bool:
    """Whether an artifact is the report (→ auto-mirrors into the task's cloud ``outputs/``).

    Only explicitly report-named artifacts (``report.md``, ``settle_report.md``) qualify; every
    other artifact (corpus, sources, drafts, scorecards, notes, logs) is an intermediate
    working product and stays in ``temp/v{N}`` (promote remains the opt-in path for those).
    """
    stem = (artifact_id or "").rsplit("/", 1)[-1].lower()
    return "report" in stem


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


class _StageMoved(Exception):
    """In-lock CAS signal for transition_stage: the stage changed after our pre-read.

    Raised inside the :meth:`ResearchService.atomic_update_project` critical section so
    the (aborted) commit writes nothing; translated to the ``CONFLICT`` outcome.
    """

    def __init__(self, stage: str) -> None:
        super().__init__(f"stage moved to {stage!r}")
        self.stage = stage


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
    def _dump_tmp(path: Path, data: Any) -> Path:
        """Serialize ``data`` durably into ``<path>.tmp`` (fsync'd) and return the tmp path.

        Split out of :meth:`_save_json` so :meth:`_txn_write` can stage *every* payload of a
        multi-file commit before any ``os.replace`` makes one visible (all-or-nothing).
        """
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            fh.write(json.dumps(data, indent=2, ensure_ascii=False, default=str))
            fh.flush()
            os.fsync(fh.fileno())
        return tmp

    @staticmethod
    def _fsync_dir(directory: Path) -> None:
        """Best-effort directory fsync (rename durability); a no-op on Windows."""
        try:
            dir_fd = os.open(directory, os.O_DIRECTORY)  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            return  # Windows / non-posix: no directory fd to fsync
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    @staticmethod
    def _save_json(path: Path, data: Any) -> None:
        """Durably persist ``data``: write ``.tmp`` -> ``fsync`` -> ``os.replace``.

        The tmp + atomic-replace ordering survives a power loss mid-write (either the old
        file or the fully written new file is present, never a torn one); ``fsync`` before
        the rename flushes the bytes to disk. The parent-directory ``fsync`` is best-effort
        (it needs ``O_DIRECTORY``, which Windows lacks).
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(ResearchService._dump_tmp(path, data), path)
        ResearchService._fsync_dir(path.parent)

    def _txn_write(self, saves: list[tuple[Path, Any]]) -> None:
        """Commit several JSON files as ONE all-or-nothing visible transaction (P3-1 c5).

        Protocol: stage *every* payload into its ``.tmp`` (fsync) BEFORE performing *any*
        ``os.replace``. If staging any tmp fails, no replace has run — the on-disk set is
        untouched (the caller's lock guarantees no interleaved writer). Because
        ``os.replace`` is atomic per file, the only crash window is *between* replaces, so
        callers pass auxiliary files first and the revision-bearing file (``project.json``)
        LAST: a half-applied commit can then never present a mutated graph under an
        un-bumped revision.
        """
        staged: list[tuple[Path, Path]] = []
        for path, data in saves:
            path.parent.mkdir(parents=True, exist_ok=True)
            staged.append((path, self._dump_tmp(path, data)))
        for path, tmp in staged:
            os.replace(tmp, path)
        for path, _tmp in staged:
            self._fsync_dir(path.parent)

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
        extra_files: list[str] | None = None,
    ) -> dict:
        """Atomically mutate an existing task's ``project.json`` (single-writer primitive).

        Every visible change to a task's authoritative state goes through this method — never
        a blind ``load -> modify -> save``. A cross-process exclusive file lock (portalocker)
        serializes the API and worker processes; inside the critical section the file is
        re-read fresh, CAS-verified against ``expected_revision`` (when given), mutated by
        ``mutate_fn``, stamped with a new monotonic ``project_revision``, and durably
        persisted (``.tmp`` -> ``fsync`` -> ``os.replace``).

        ``extra_files`` (P3-1): sibling files in the same project dir (e.g.
        ``["graph.json"]``) that must commit *together* with ``project.json`` as one
        transaction. Each is loaded fresh inside the lock and handed to
        ``mutate_fn(project, extras)`` as a ``name -> value`` dict (``None`` when the file
        does not exist yet — the mutation owns the default). The commit goes through
        :meth:`_txn_write` with the extras replaced FIRST and ``project.json`` LAST, so a
        crash can never expose a mutated graph under an un-bumped revision (constraint 5).
        A ``mutate_fn`` that returns ``False`` signals "nothing changed": NOTHING is
        written and the revision is NOT bumped (transaction-level idempotency,
        constraint 6) — the caller still receives the current project dict.

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
                if extra_files is not None:
                    extras = {
                        name: self._load_json(project_dir / name, None)
                        for name in extra_files
                    }
                    if mutate_fn(project, extras) is False:
                        return dict(project)  # no-op replay: zero writes, zero bump
                    project["updated_at"] = _now_iso()
                    project["project_revision"] = project.get("project_revision", 0) + 1
                    self._txn_write(
                        [(project_dir / name, extras[name]) for name in extra_files]
                        + [(path, project)]  # revision-bearing file replaced LAST
                    )
                else:
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
            "run_version": None,        # run_seq of the current run (drives temp/vN + outputs/_vN)
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
            # Per-run transient ledger of cloud folder/asset ids created for THIS run's
            # temp/vN + scrape/ projection (red line 3): reset every begin_run, never a
            # multi-run index. ``progress_cursor`` is the last run_events seq the worker has
            # already drained into the bound session chat.
            "cloud_assets": {},
            "progress_cursor": 0,
        }

    @staticmethod
    def _run_version_of(project: dict) -> int | None:
        """The current run's ``run_seq`` (``driver.run_version``), or ``None`` outside a run.

        A value of ``None`` means the versioned temp/vN + outputs/_vN layout does not apply and
        callers must fall back to the legacy in-place ``outputs/`` behaviour.
        """
        driver = project.get("driver")
        rv = driver.get("run_version") if isinstance(driver, dict) else None
        return rv if isinstance(rv, int) else None

    @staticmethod
    def _assets_ledger(project: dict) -> dict:
        """The current run's ``cloud_assets`` transient ledger (never ``None``)."""
        driver = project.get("driver")
        if not isinstance(driver, dict):
            driver = project["driver"] = {}
        ledger = driver.setdefault("cloud_assets", {})
        if not isinstance(ledger, dict):
            ledger = driver["cloud_assets"] = {}
        return ledger

    @staticmethod
    def _cloud_asset_key(artifact_id: str) -> str:
        """A ``cloud_assets`` key for one artifact that can never collide with the ``_dirs`` sub-dict."""
        return f"_a:{artifact_id}"

    @staticmethod
    def _scrape_tally(project: dict) -> int:
        """How many scrape files this run has captured so far (0 when none / not a run).

        Scrape saves never announce themselves; totals are folded into stage-summary events so
        the user still sees capture volume without per-save chat spam (safety rule 3).
        """
        ledger = project.get("driver", {}).get("cloud_assets") if isinstance(project.get("driver"), dict) else None
        counts = ledger.get("_scrape_counts") if isinstance(ledger, dict) else None
        if not isinstance(counts, dict):
            return 0
        return sum(int(v) for v in counts.values())

    async def _ensure_cloud_dir(
        self, owner_id: uuid.UUID, project: dict, cloud_rel_dir: str
    ) -> str | None:
        """Get-or-create the cloud folder row at ``<task folder>/{cloud_rel_dir}`` (idempotent).

        Only meaningful for a versioned run (temp/vN + scrape need real folder rows so the
        working-directory tree shows them). Walks each path segment under the task's cloud
        folder, reusing an existing row when one is already there and creating only the missing
        levels — never creating a duplicate same-name folder (red line 7). Returns the folder id
        of the deepest requested directory, or ``None`` when the task has no cloud projection or
        no versioned run (the caller then falls back to legacy behaviour).
        """
        cloud_root = project.get("cloud_folder_path")
        rv = self._run_version_of(project)
        if not cloud_root or not rv:
            return None
        ledger = self._assets_ledger(project)
        dirs = ledger.setdefault("_dirs", {})
        if not isinstance(dirs, dict):
            dirs = ledger["_dirs"] = {}
        rel = ""
        folder_id: str | None = None
        for part in cloud_rel_dir.split("/"):
            rel = f"{rel}/{part}" if rel else part
            folder_id = dirs.get(rel)
            if folder_id:
                continue
            parent_path = f"{cloud_root}/{rel.rsplit('/', 1)[0]}" if "/" in rel else cloud_root
            full = f"{cloud_root}/{rel}"
            existing = [
                f for f in await self.drive.list_folders(owner_id) if f.get("path") == full
            ]
            if existing:
                folder_id = str(existing[0]["id"])
            else:
                created = await self.drive.create_folder(owner_id, None, parent_path, part)
                folder_id = str(created["id"])
            dirs[rel] = folder_id
        return folder_id

    def _merge_cloud_assets(
        self, owner_id: uuid.UUID, project_id: str, additions: dict
    ) -> None:
        """Merge ``additions`` (top-level cloud_assets keys) into the driver ledger atomically.

        The merge re-reads the project under the lock so a concurrently-updated checkpoint is
        preserved, and nested dicts are merged (never clobbered). Best-effort persistence: the
        mirror's authoritative path stays the artifact record, so a failure only means the
        ledger is rebuilt next time (the folder/asset already exists and is re-found).
        """
        def mutate(project: dict) -> None:
            ledger = project.setdefault("driver", {}).setdefault("cloud_assets", {})
            if not isinstance(ledger, dict):
                ledger = project["driver"]["cloud_assets"] = {}
            for key, value in additions.items():
                current = ledger.get(key)
                if isinstance(current, dict) and isinstance(value, dict):
                    current.update(value)
                else:
                    ledger[key] = value

        try:
            self.atomic_update_project(owner_id, project_id, mutate)
        except Exception as exc:  # noqa: BLE001 - best-effort ledger, never break the caller
            logger.debug(
                "research cloud-assets ledger merge failed for %s (rebuilt lazily): %s",
                project_id, exc,
            )

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
        execution_mode: str = "strict",
        idempotency_key: str | None = None,
    ) -> dict:
        if execution_mode not in ("strict", "progressive"):
            raise ValueError(
                f"unknown execution_mode {execution_mode!r} (expected 'strict' | 'progressive')"
            )
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
                    "execution_mode": existing.get("execution_mode", "strict"),
                    "idempotent": True,
                }
        project = {
            "id": str(uuid.uuid4()),
            "owner_id": str(owner_id),
            "name": name,
            "profile": profile,
            "execution_mode": execution_mode,
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
            "execution_mode": execution_mode,
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
            "execution_mode": project.get("execution_mode", "strict"),
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
            "execution_mode": project.get("execution_mode", "strict"),
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
        new_artifact = v1 is None  # only a genuinely-new artifact is a progress event (not an overwrite)
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
        # Project the draft into the task's cloud projection (best-effort; scratch is the
        # authority). In a versioned run the working copy lands in temp/vN and the id is kept on
        # the run ledger, not this record; legacy tasks keep the in-place outputs/ id on record.
        await self._mirror_output(owner_id, project, record, content)
        self._save_json(
            self._artifact_dir(owner_id, project_id, artifact_id) / "v1", record
        )
        if new_artifact:
            self._log_run_event(
                owner_id,
                project_id,
                event_type="artifact",
                key=f"artifact:{artifact_id}",
                detail=f"new artifact '{artifact_id}' v1 written",
                stage=project.get("stage"),
                project=project,
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
            rv = self._run_version_of(project)
            if rv:
                # Versioned task path: promotion CREATES a NEW versioned final asset
                # ``outputs/<stem>_v{rv}.md`` (Create-New — never Move/Rename or reuse the
                # temp/vN working copy, which stays intact; red line 2). The run ledger holds
                # the promoted asset so single-run retries are strongly idempotent: a re-promote
                # in the SAME run (even after a new scratch version) updates that one asset in
                # place instead of minting ``_v{rv+1}`` (safety rule 5); only a NEW begin_run
                # moves the version forward.
                return await self._promote_run_asset(
                    owner_id, project, project_id, record, version_paths[-1],
                    artifact_id=artifact_id,
                    run_version=rv,
                    promote_idempotency_key=promote_idempotency_key,
                )
            # Legacy task path: the report lives in the cloud outputs/ projection already.
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

    async def _promote_run_asset(
        self,
        owner_id: uuid.UUID,
        project: dict,
        project_id: str,
        record: dict,
        record_path: Path,
        *,
        artifact_id: str,
        run_version: int,
        promote_idempotency_key: str | None,
    ) -> dict:
        """Create/refresh the ONE versioned final asset for this artifact+run in ``outputs/``.

        First promote of a run mints ``outputs/<stem>_v{run_version}.md`` (Create-New). Any
        later promote inside the SAME run — including after the agent mints a new scratch
        version — reuses that same asset (``update_content`` in place), so a run never produces
        a second ``_v{N}`` file. The promoted id + path live on the run's ``cloud_assets``
        ledger (persisted only when newly created).
        """
        cloud_root = project["cloud_folder_path"]
        ledger = self._assets_ledger(project)
        key = self._cloud_asset_key(artifact_id)
        entry = ledger.setdefault(key, {})
        if not isinstance(entry, dict):
            entry = ledger[key] = {}
        out_asset = entry.get("out_asset")
        out_name = entry.get("out_name")
        newly_created = False
        if out_asset and out_name:
            # Same-run re-promote after a new scratch version: refresh in place, never a v+1.
            await self.drive.update_content(owner_id, uuid.UUID(out_asset), record["content"])
        else:
            out_name = f"{_report_stem(artifact_id)}_v{run_version}.md"
            asset = await self.drive.save_artifact(
                owner_id,
                name=out_name,
                mime_type="text/markdown",
                content=record["content"].encode("utf-8"),
                folder_path=f"{cloud_root}/outputs",
                workspace_id=None,
            )
            out_asset = str(asset.id)
            entry["out_asset"] = out_asset
            entry["out_name"] = out_name
            entry["out_path"] = f"{cloud_root}/outputs/{out_name}"
            newly_created = True
        await self.drive.mark_rag_pending(uuid.UUID(out_asset))
        record["status"] = "PROMOTED"
        record["drive_asset_id"] = out_asset
        record["drive_path"] = f"{cloud_root}/outputs/{out_name}"
        record["rag_status"] = "PENDING"
        record["promote_idempotency_key"] = promote_idempotency_key
        record["updated_at"] = _now_iso()
        self._save_json(record_path, record)
        if newly_created:
            self._merge_cloud_assets(owner_id, project_id, {key: entry})
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
        # ``cloud_output_asset_id`` is carried over for the LEGACY no-run projection so it is
        # updated in place. Under a versioned run the mirror id lives on the run ledger (never
        # shared across runs), so this field is simply untouched (None) there.
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
        self._log_run_event(
            owner_id,
            project_id,
            event_type="artifact",
            key=f"artifact:{artifact_id}:v{next_version}",
            detail=f"artifact '{artifact_id}' v{next_version} written",
            stage=project.get("stage"),
            project=project,
        )
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
            "execution_mode": project.get("execution_mode", "strict"),
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
        execution_mode: str = "strict",
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
        if execution_mode not in ("strict", "progressive"):
            raise ValueError(
                f"unknown execution_mode {execution_mode!r} (expected 'strict' | 'progressive')"
            )
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
            "execution_mode": execution_mode,
            "status": "ACTIVE",
            "stage": "DISCOVER",
            "gates": {gate: "NOT_RUN" for gate in _GATES},
            "idempotency_key": idempotency_key,
            "cloud_folder_id": cloud_folder_id,
            "cloud_folder_path": cloud_folder["path"],
            "materials": [],
            "cloud_mirrors": {},
            "run_seq": 0,               # monotonic per-run version (bumped atomically in begin_run)
            "project_revision": 0,      # monotonic version; bumped by every atomic commit
            "driver": None,             # driver checkpoint (materialized on first run)
            "last_block": None,         # terminal-stop record {kind, reason, at, execution_id}
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
        }
        try:
            # The user-visible task folder always carries the three work folders, even before
            # any material is copied / run begun / artifact promoted, so the working-directory
            # layout is stable from the moment the task exists (a task with no materials,
            # outputs or temp run yet still shows all three in the drive). ``temp/`` holds one
            # per-run ``v{N}`` subfolder (created lazily by the first run). Get-or-create by
            # exact path so a stale/trashed same-name row can never leave the task folder
            # missing ``outputs/`` (the folder the user's reports must land in).
            existing_paths = {
                f.get("path") for f in await self.drive.list_folders(owner_id)
            }
            for sub in ("materials", "outputs", "temp"):
                if f"{cloud_folder['path']}/{sub}" not in existing_paths:
                    await self.drive.create_folder(owner_id, None, cloud_folder["path"], sub)
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
        """Project one artifact's scratch content into the task's cloud drive.

        Two shapes (chat-task path only; a task without a ``cloud_folder_path`` is a no-op):

        - **Versioned run** (``driver.run_version`` set): the working copy mirrors into
          ``temp/v{N}/<artifact_id>.md``. Report artifacts additionally mirror into
          ``outputs/<task name>.md`` (same folder/ledger mechanism, no version suffix). The
          run's ``cloud_assets`` ledger holds the temp/out asset ids so a same-run rewrite
          updates in place and a *later* run writes a new file (the ids are
          deliberately NOT stored on the shared version record — that would leak one run's
          asset id into the next run). ``record["cloud_output_asset_id"]`` stays untouched.
        - **Legacy / no run**: the previous in-place ``outputs/<artifact_id>.md`` projection
          with the asset id carried on the record (unchanged behaviour).

        Best-effort like :meth:`_mirror_cloud`: scratch ``artifacts/`` is always authoritative,
        so a drive failure only logs. Returns ``True`` when a new cloud asset was created.
        """
        cloud_folder_path = project.get("cloud_folder_path")
        if not cloud_folder_path:
            return False
        rv = self._run_version_of(project)
        if rv:
            ledger = self._assets_ledger(project)
            dirs = ledger.setdefault("_dirs", {})
            if not isinstance(dirs, dict):
                dirs = ledger["_dirs"] = {}
            key = self._cloud_asset_key(record["artifact_id"])
            entry = ledger.setdefault(key, {})
            if not isinstance(entry, dict):
                entry = ledger[key] = {}

            async def _mirror(rel_dir: str, out_name: str, asset_field: str) -> bool:
                """Create-or-update one cloud asset under ``<task folder>/<rel_dir>``.

                Returns ``True`` when a new asset was created. Raises on drive failure so the
                caller can log per-target and keep the other mirror independent.
                """
                if dirs.get(rel_dir) is None and await self._ensure_cloud_dir(owner_id, project, rel_dir) is None:
                    raise RuntimeError(f"cloud folder {rel_dir} unavailable")
                asset_id = entry.get(asset_field)
                if asset_id:
                    await self.drive.update_content(owner_id, uuid.UUID(asset_id), content)
                    return False
                asset = await self.drive.save_artifact(
                    owner_id,
                    name=out_name,
                    mime_type="text/markdown",
                    content=content.encode("utf-8"),
                    folder_path=f"{cloud_folder_path}/{rel_dir}",
                    workspace_id=None,
                )
                entry[asset_field] = str(asset.id)
                return True

            created_new = False
            try:
                created_new |= await _mirror(
                    f"temp/v{rv}", f"{_safe_filename(record['artifact_id'])}.md", "temp_asset"
                )
            except Exception:
                logger.exception("research run-temp mirror failed for %s", record["artifact_id"])
                return False
            if _is_report_artifact(record["artifact_id"]):
                try:
                    # Filename = the task name (no version suffix): the first report write of
                    # a run creates ``outputs/<task name>.md``; later writes of the SAME run
                    # update it in place (``out_asset``); a NEW run's report lands as a new
                    # file (fresh ledger → ``save_artifact`` auto-suffixes the busy name).
                    created_new |= await _mirror(
                        "outputs",
                        f"{_safe_filename(project.get('name') or 'report')}.md",
                        "out_asset",
                    )
                except Exception:
                    logger.exception("research report-output mirror failed for %s", record["artifact_id"])
            self._merge_cloud_assets(
                owner_id, record["project_id"], {"_dirs": dirs, key: entry}
            )
            return created_new
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

        Two P0 guards block deletion: a live agent run (the ``active_run`` slot — the mutex
        over the agent) and a report the knowledge base already indexed (remove it from RAG
        first). The request is recorded in ``project.json`` *before* teardown so a crash
        mid-delete is auditable. The cloud folder goes to Trash (soft delete); restoring it
        does NOT resurrect the task — scratch is hard-deleted, so the task state is gone.
        """
        project = self._load_project(owner_id, task_id)  # 404 if missing / traversal

        # P0: never delete while the agent is mid-execution. The server-owned run slot
        # (``active_run``) is the live mutex; a run holds it from ``begin_run`` until the
        # turn fully ends (``end_run``), so it alone distinguishes a live run from a dead
        # one. Per-tool RUNNING execution rows are NOT an independent signal: they can be
        # left behind when a run stops mid-step (the slot is released but the interrupted
        # tool call was never finalized), and blocking on them makes such a task impossible
        # to delete even though nothing is running. Those orphans are wiped by the teardown
        # below, so only a live slot blocks.
        if project.get("active_run") and project["active_run"].get("status") == "RUNNING":
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
        out = {
            "project_id": project["id"],
            "stage": project["stage"],
            "status": project["status"],
            "gates": project["gates"],
        }
        # P1-C: a tiny Claim digest so a fresh (auto) turn can SEE the claims an
        # earlier turn already recorded — cross-turn context is cleared and no other
        # action lists node ids, which is what let a shadow k*-set be minted in
        # EVIDENCE. ``id`` is the sole identity anchor; ``label`` (first 80 chars) is
        # display-only semantic context, and ``anchored`` mirrors the CLAIM_GATE
        # predicate (has citations) so the model can tell "reuse + verify/mutate" from
        # "create the missing ones". P3-1: ``evidence_fingerprint`` + ``pending`` extend
        # the same digest with the delta-pending verdict (batch.py constraint-2 rule read
        # against this run's ``_verify_fps`` baseline) so a fresh turn can skip
        # already-committed claims BEFORE issuing verify_batch work. Conditional
        # emission: with no claims the payload is the old contract. Never a full graph
        # dump.
        graph = self._load_graph(owner_id, project_id)
        stored_fps = ResearchService._assets_ledger(project).get("_verify_fps")
        if not isinstance(stored_fps, dict):
            stored_fps = {}
        claims = []
        for n in graph["nodes"]:
            if n.get("type") != "Claim":
                continue
            fp = evidence_fingerprint(graph, n["id"])
            claims.append({
                "id": n["id"],
                "label": (n.get("label") or "")[:80],
                "anchored": bool(n.get("citations")),
                "evidence_fingerprint": fp,
                "pending": compute_pending(stored_fps.get(n["id"]), fp,
                                           _batch_gate_ok(n, normalize_claim_strength)),
            })
        if claims:
            out["claims"] = claims
        return out

    def transition_stage(
        self,
        owner_id: uuid.UUID,
        project_id: str,
        *,
        target: str,
        expected_current_stage: str | None = None,
    ) -> dict:
        """Move the task one legal stage forward, reporting the outcome as one of seven states.

        ``result["transition"]`` is the authoritative verb; ``granted`` stays as the
        backward-compatible summary (True only for the two benign outcomes):

        - ``ADVANCED``          — the stage move COMMITTED (atomic write finished). This is
          the only outcome that ends the current agent turn: the runtime is asked to stop
          via the generic ``AgentTurn.request_stop`` contract, so the next turn opens at the
          new stage instead of re-doing the old one. Never reported unless the commit
          succeeded.
        - ``ALREADY_AT_TARGET`` — benign no-op re-request (idempotent); nothing written, the
          turn continues.
        - ``NOT_READY``         — strict mode, guarding gate has never been checked.
        - ``GATE_BLOCKED``      — strict mode, guarding gate is FAIL (needs a pass or a
          human override; never bypass it here).
        - ``CONFLICT``          — the stage moved concurrently (the in-lock CAS failed).
        - ``ILLEGAL``           — unknown stage, or not the single legal next stage.
        - ``ERROR``             — persistence failed (raises as before for missing projects).
        """
        project = self._load_project(owner_id, project_id)
        current = project["stage"]
        if target not in _STAGES:
            return {
                "requested": target, "transition": "ILLEGAL", "granted": False, "stage": current,
                "reason": f"unknown stage {target!r}",
            }
        if current == target:
            # Re-requesting the stage we already occupy is a benign outcome, not an error:
            # answering it as "illegal" made the model retry with other guesses and burn
            # turns. Nothing is written and the turn is NOT stopped.
            return {
                "requested": target, "transition": "ALREADY_AT_TARGET", "granted": True,
                "stage": current,
                "note": "stage unchanged; continue this stage's work",
            }
        if expected_current_stage is not None and current != expected_current_stage:
            return {
                "requested": target, "transition": "CONFLICT", "granted": False, "stage": current,
                "reason": f"stage moved: expected {expected_current_stage!r}, found {current!r}",
            }
        if _LEGAL_NEXT.get(current) != target:
            return {
                "requested": target, "transition": "ILLEGAL", "granted": False, "stage": current,
                "reason": f"illegal transition {current} -> {target}",
            }
        gate = _GATE_BEFORE.get(target)
        gate_state = project["gates"].get(gate) if gate else None
        progressive = project.get("execution_mode", "strict") == "progressive"
        if gate and gate_state not in ("PASS", "OVERRIDE") and not progressive:
            # Strict (the default): an un-passed guard blocks the transition — semantics
            # unchanged; only the reason is now split into its own verbs (never checked
            # vs checked-and-failed).
            return {
                "requested": target,
                "transition": "NOT_READY" if gate_state in (None, "NOT_RUN") else "GATE_BLOCKED",
                "granted": False, "stage": current, "gate": gate,
                "reason": f"transition {current} -> {target} guarded by {gate}: "
                f"not passed (current {gate_state or 'NOT_RUN'})",
            }

        # Progressive: the SAME deterministic checks still run (read-only, never writes a
        # verdict) and the gate's own state is never forged to PASS — only the blocking
        # consequence of a FAIL is lifted, by recording the failure into
        # ``project["diagnostics"]`` and advancing the stage in the SAME atomic commit.
        # No de-dup machinery is needed: ``_LEGAL_NEXT`` is a strictly forward chain with
        # no backtracking, so each guarded target (EXECUTE/EXPLAIN/REVIEW/REPRODUCE) can
        # be transitioned into at most once per run — a (gate, stage) diagnostic is
        # structurally unique.
        diagnosed: list[dict] | None = None
        if gate and gate_state not in ("PASS", "OVERRIDE") and progressive:
            checks = self._gate_checks_readonly(owner_id, project_id, gate)
            diagnosed = [c for c in checks if not c.get("ok")]

        def mutate(p: dict) -> None:
            # CAS closed inside the lock critical section (atomic_update_project): if the
            # stage moved between the pre-read above and our commit, abort without writing.
            if p["stage"] != current:
                raise _StageMoved(p["stage"])
            p["stage"] = target
            if diagnosed:
                p.setdefault("diagnostics", []).append(
                    {
                        "gate": gate,
                        "stage": current,
                        "target": target,
                        "failed_checks": diagnosed,
                        "timestamp": _now_iso(),
                    }
                )

        try:
            project = self.atomic_update_project(owner_id, project_id, mutate)
        except _StageMoved as moved:
            return {
                "requested": target, "transition": "CONFLICT", "granted": False,
                "stage": moved.stage,
                "reason": f"stage moved concurrently (now {moved.stage!r})",
            }

        # ── ADVANCED: the commit succeeded; only now may we announce it ──
        self._log_stage_event(
            owner_id, project_id, current=current, target=target, project=project
        )
        if diagnosed:
            names = ", ".join(c["name"] for c in diagnosed)
            self._log_run_event(
                owner_id,
                project_id,
                event_type="gate_diagnostic",
                key=f"gate:{gate}→{target}",
                stage=target,
                detail=f"{gate} failed ({len(diagnosed)}): {names}",
                project=project,
            )
        # Turn convergence through the GENERIC runtime contract: the loop never learns the
        # word "stage" — it only honors turn.stop_requested at the step boundary, so the
        # rest of this turn cannot keep re-doing the finished stage's work.
        turn = current_turn()
        if turn is not None:
            turn.request_stop("stage_advanced")
        return {
            "requested": target,
            "transition": "ADVANCED",
            "granted": True,
            "stage": project["stage"],
            "gate": gate,
            "diagnosed": diagnosed or [],
        }

    def _log_stage_event(
        self,
        owner_id: uuid.UUID,
        project_id: str,
        *,
        current: str,
        target: str,
        project: dict,
    ) -> None:
        """Record one ``stage`` progress event for a granted transition (run-scoped, idempotent).

        The detail carries this run's scrape tally when any capture happened, so per-save scrape
        writes stay silent but the stage boundary still reports volume (safety rule 3).
        """
        tally = self._scrape_tally(project)
        suffix = f" · {tally} scrape{'s' if tally != 1 else ''} saved" if tally else ""
        self._log_run_event(
            owner_id,
            project_id,
            event_type="stage",
            key=f"stage:{target}",
            stage=target,
            detail=f"{current} → {target}{suffix}",
            project=project,
        )

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
        # P3-3 boundary repair: a JSON-encoded object string is un-wrapped ONCE and then
        # flows through the unchanged checks below — a repaired record is identical to a
        # native one; the repair itself is audited via the ``coerced`` tag (never the raw
        # payload). Anything unparseable / non-dict falls through to the same TypeError.
        node, _node_coerced = _coerce_json_container(node, expect="dict")
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
            return _with_coerced({"node": existing, "idempotent": True}, _node_coerced, "node")
        record = {
            "id": node_id,
            "type": node_type,
            "label": node.get("label", node_id),
            "status": node.get("status", "VALID"),
            **{k: v for k, v in node.items() if k not in ("id", "type", "label", "status")},
        }
        if record["type"] == "Claim" and record.get("strength") is not None:
            # Boundary validation (single ingestion point): a strength outside the canonical
            # set is refused with a repair hint instead of being stored and silently
            # dooming CLAIM_GATE later. Report-style high/medium/low are accepted, stored
            # normalized.
            norm = normalize_claim_strength(record["strength"])
            if norm is None:
                raise _claim_strength_error(record["strength"])
            record["strength"] = norm
        graph["nodes"].append(record)
        self._save_graph(owner_id, project_id, graph)
        return _with_coerced({"node": record, "idempotent": False}, _node_coerced, "node")

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
        if target["type"] == "Claim" and "strength" in patch:
            norm = normalize_claim_strength(patch["strength"])
            if norm is None:
                raise _claim_strength_error(patch["strength"])
            patch = {**patch, "strength": norm}
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
        # Read-side normalization: strengths stored before the alias table existed (raw
        # "high"/"medium") count as valid here, so legacy graphs are not held hostage by
        # the vocabulary fix.
        ok = bool(claims) and all(
            c.get("citations") and normalize_claim_strength(c.get("strength")) is not None
            for c in claims
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
        if not scorecard:
            # Nothing in the current toolset writes a scorecard, so "missing" is a
            # provisioning gap, not agent misconduct: tagged ``diagnostic_only`` so
            # progressive diagnostics and review notes can tell it apart from a real
            # scorecard FAIL. Strict blocking semantics are unchanged (ok stays False).
            return [
                {
                    "name": "scorecard",
                    "ok": False,
                    "severity": "diagnostic_only",
                    "detail": "scorecard_missing: no scorecard has been recorded for this "
                    "project (the >=7-rows / no-fatal bar cannot be evaluated)",
                }
            ]
        ok = len(scorecard) >= 7 and not any(row.get("fatal") for row in scorecard)
        return [
            {
                "name": "scorecard",
                "ok": ok,
                "severity": "blocking",
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
        new_edition: bool = False,
    ) -> dict:
        """Acquire the single active-run slot for a task (mutex over concurrent turns).

        One task may hold at most one live run at a time (T4 invariant #2: concurrent
        Task A/Task B runs are fine, but a single task never runs two turns in parallel).
        A RUNNING slot older than ``stale_after_seconds`` is presumed dead — the owning
        process crashed before ``end_run`` — and is adopted: any RUNNING executions it
        left behind are flipped to ABORTED so the delete guard never blocks forever.

        ``new_edition`` (the desktop "Run" control on a task that already reached the
        terminal PUBLISH stage): PUBLISH has no legal next stage, so a plain resume would
        stop at turn 0 and produce nothing. This run instead starts a NEW edition — the
        run-scoped shared state is reset (stage -> DISCOVER, gates -> NOT_RUN, diagnostics
        and last_block cleared, evidence graph emptied) so the driver actually drives
        DISCOVER -> ... -> PUBLISH again. ``run_seq`` still advances (red line 1), so this
        edition's working copy lands in temp/v{N} + outputs/_v{N}; earlier editions' files
        are versioned and are never touched. For a task short of PUBLISH (mid-chain /
        blocked resume) the flag is a no-op and the run resumes from the current stage.

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
            # Atomically mint the next per-run version (red line 1). Legacy tasks without the
            # field fall back to ``get("run_seq", 0) + 1`` — no wholesale migration. The
            # version stamps every temp/vN + outputs/_vN folder/asset for THIS run.
            run_seq = project.get("run_seq", 0) + 1
            project["run_seq"] = run_seq
            if new_edition and project.get("stage") == "PUBLISH":
                # Re-run of a task that already finished → start a NEW edition. PUBLISH is a
                # terminal stage with no legal next, so a resume can never leave it; resetting
                # the run-scoped shared state is what lets this run drive DISCOVER→…→PUBLISH
                # again. Only run-scoped state is touched — the finished edition's artifacts
                # (artifacts/<id>/vN), temp/vN + outputs/_vN and event logs stay intact, and
                # run_seq above has already moved the version forward for this new edition.
                project["stage"] = "DISCOVER"
                project["gates"] = {g: "NOT_RUN" for g in _GATES}
                project["diagnostics"] = []
                project["last_block"] = None
                project["status"] = "ACTIVE"
                # The evidence graph is per-task shared state (graph.json, not versioned):
                # empty it so the new edition re-gathers sources instead of inheriting the
                # finished edition's STALE/CANDIDATE nodes as if they were current evidence.
                self._save_graph(owner_id, project_id, {"nodes": [], "edges": []})
            # Reset the driver ledger for this run: a new run = a new run_id, and the old
            # run's turn/cost/no-progress state must never leak into it.
            project["driver"] = {
                **self._empty_driver(),
                "run_id": run_id,
                "run_version": run_seq,
                "turn_state": "done",
                "started_at": _now_iso(),
                "updated_at": _now_iso(),
                # L2 definition stamp: mint the workflow-definition fingerprint at
                # acquisition; every later turn re-checks it (driver.auto_turn) and a
                # mismatch terminalizes the run as definition drift — never a silent
                # resume under a rewritten flow.
                "definition_fp": RESEARCH_WORKFLOW.fingerprint(),
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

    # ── run progress events (append-only scratch log, drained by the worker) ──
    # Events are produced AT the point a milestone commits (never by a poller) and persist to
    # ``<task>/run_events.json``, which is semantically append-only: rows are never deleted,
    # every row carries a monotonic ``seq`` + unique ``event_id``, and the driver checkpoint's
    # ``progress_cursor`` is the only "consumed up to" marker (red line 4 + safety rule 1).
    # The worker drains new rows into the bound session chat as ``system`` messages.

    def _run_events_path(self, owner_id: uuid.UUID, project_id: str) -> Path:
        return self._project_dir(owner_id, project_id) / _RUN_EVENTS_FILE

    def _log_run_event(
        self,
        owner_id: uuid.UUID,
        project_id: str,
        *,
        event_type: str,
        key: str,
        detail: str = "",
        stage: str | None = None,
        project: dict | None = None,
    ) -> int | None:
        """Append one progress event, deduplicated by ``(run_seq, event_type, key)``.

        No-op outside a versioned run (skills / plain projects have no run_seq). Returns the
        event's ``seq`` when recorded (or found already recorded), else ``None``.
        """
        if event_type not in _RUN_EVENT_TYPES:
            raise ValueError(f"unknown run event type: {event_type}")
        if project is None:
            project = self._load_project(owner_id, project_id)
        run_seq = self._run_version_of(project)
        if not run_seq:
            return None
        path = self._run_events_path(owner_id, project_id)
        data = self._load_json(path, {"events": []})
        events = data["events"]
        for event in events:
            if (
                event.get("run_seq") == run_seq
                and event.get("type") == event_type
                and event.get("key") == key
            ):
                return event["seq"]
        seq = max((e["seq"] for e in events), default=0) + 1
        events.append(
            {
                "seq": seq,
                "event_id": str(uuid.uuid4()),
                "run_seq": run_seq,
                "type": event_type,
                "key": key,
                "stage": stage,
                "detail": detail,
                "ts": _now_iso(),
            }
        )
        self._save_json(path, {"events": events})
        return seq

    def append_run_event(
        self,
        owner_id: uuid.UUID,
        project_id: str,
        *,
        event_type: str,
        key: str,
        detail: str = "",
        stage: str | None = None,
    ) -> int | None:
        """Public append (worker terminal events + tests); delegates to :meth:`_log_run_event`."""
        return self._log_run_event(
            owner_id, project_id, event_type=event_type, key=key, detail=detail, stage=stage
        )

    @staticmethod
    def _render_run_event(event: dict) -> str:
        """One compact human line for a run event (what the worker surfaces as a system row)."""
        run_seq = event.get("run_seq")
        tag = f"[auto-run v{run_seq}]"
        etype = event["type"]
        detail = event.get("detail") or ""
        if etype == "stage":
            return f"{tag} stage → {event.get('stage') or detail}: {detail}"
        if etype == "gate_diagnostic":
            return f"{tag} gate recorded: {detail}"
        if etype == "artifact":
            return f"{tag} {detail}"
        if etype == "terminal":
            return f"{tag} {detail}"
        return f"{tag} {etype}: {detail}"

    async def drain_run_events(
        self,
        session_factory,
        owner_id: uuid.UUID,
        project_id: str,
        session_id: str | None,
    ) -> int:
        """Surface un-consumed run events of the CURRENT run into the bound session chat.

        **Worker-only bridge** (this is where DB/session writes belong, never inside the state
        machine or a tool monitor wrapper). Reads rows newer than ``driver.progress_cursor``,
        writes each as one ``system`` message via ``insert_plain_message`` + a best-effort
        task mirror, then advances the cursor past everything it *committed*. A DB failure on a
        row leaves the cursor BEFORE that row so the next drain retries it — events already
        committed are never re-emitted (no duplicates). Rows left over from an older run are
        skipped (they were never this run's to show) but still consume the cursor.
        """
        if not session_id:
            return 0
        project = self._load_project(owner_id, project_id)
        driver = project.get("driver") or {}
        run_seq = driver.get("run_version")
        if not isinstance(run_seq, int):
            return 0
        cursor = int(driver.get("progress_cursor", 0) or 0)
        events = self._load_json(self._run_events_path(owner_id, project_id), {"events": []})
        newer = [e for e in events["events"] if int(e["seq"]) > cursor]
        if not newer:
            return 0
        from core.infrastructure.memory import insert_plain_message  # local: worker-only bridge

        written = 0
        committed = cursor
        for event in sorted(newer, key=lambda e: e["seq"]):
            if event.get("run_seq") != run_seq:
                committed = max(committed, int(event["seq"]))
                continue
            text = self._render_run_event(event)
            try:
                await insert_plain_message(
                    session_factory, owner_id, uuid.UUID(session_id), "system", text
                )
            except Exception as exc:  # noqa: BLE001 - DB write is best-effort, never blocks
                logger.warning(
                    "research drain insert failed for event %s (cursor kept): %s",
                    event["seq"], exc,
                )
                break
            written += 1
            committed = max(committed, int(event["seq"]))
            try:
                await self.append_session_turn(owner_id, session_id, "system", text)
            except Exception as exc:  # noqa: BLE001 - advisory mirror
                logger.debug("research drain mirror failed for %s: %s", event["seq"], exc)
        if committed != cursor:
            try:
                self.set_driver_checkpoint(
                    owner_id, project_id, patch={"progress_cursor": committed}
                )
            except Exception as exc:  # noqa: BLE001 - a stale cursor only re-drains (deduped)
                logger.warning("research drain cursor write failed for %s: %s", project_id, exc)
        return written

    # ── research_scrape (agent-invocable raw-source capture, silent by contract) ──
    async def save_scrape(
        self,
        owner_id: uuid.UUID,
        project_id: str,
        *,
        source: str,
        url: str,
        query: str,
        content: str,
    ) -> dict:
        """Store one retrieved page's *content* into ``temp/v{N}/scrape/<source>_<query>_<n>.md``.

        Agent-invocable capture of what a web/social search actually returned. Writes a small
        structured metadata header then the extracted body text — never the underlying response
        JSON. **Silent**: no run event, no chat line, no monitor wake-up per save (safety rule 3);
        totals surface only in the stage-summary event a transition emits. No-op (returns
        ``saved: False``) outside a versioned cloud task.
        """
        project = self._load_project(owner_id, project_id)
        cloud_root = project.get("cloud_folder_path")
        rv = self._run_version_of(project)
        if not cloud_root or not rv:
            return {"saved": False, "path": None, "reason": "no versioned cloud task run"}
        rel_dir = f"temp/v{rv}/{_SCRAPE_SUBDIR}"
        try:
            await self._ensure_cloud_dir(owner_id, project, rel_dir)
        except Exception as exc:  # noqa: BLE001 - best-effort, never fail the agent's turn
            logger.warning("research scrape dir ensure failed for %s: %s", project_id, exc)
            return {"saved": False, "path": None, "reason": "scrape folder unavailable"}
        ledger = self._assets_ledger(project)
        counts = ledger.setdefault("_scrape_counts", {})
        if not isinstance(counts, dict):
            counts = ledger["_scrape_counts"] = {}
        seq_key = f"{source}|{query}"
        seq = int(counts.get(seq_key, 0)) + 1
        header = (
            f"<!-- DeepDive research scrape\n"
            f"Source: {source}\n"
            f"URL: {url}\n"
            f"Query: {query}\n"
            f"Retrieved_at: {_now_iso()}\n"
            f"Run_version: {rv}\n"
            f"-->\n\n"
        )
        name = f"{_safe_filename(source or 'source')}_{_slug_token(query)}_{seq}.md"
        try:
            asset = await self.drive.save_artifact(
                owner_id,
                name=name,
                mime_type="text/markdown",
                content=(header + (content or "")).encode("utf-8"),
                folder_path=f"{cloud_root}/{rel_dir}",
                workspace_id=None,
            )
        except Exception as exc:  # noqa: BLE001 - best-effort, never fail the agent's turn
            logger.warning("research scrape save failed for %s: %s", project_id, exc)
            return {"saved": False, "path": None, "reason": "drive write failed"}
        counts[seq_key] = seq
        self._merge_cloud_assets(owner_id, project_id, {"_scrape_counts": counts})
        return {
            "saved": True,
            "path": f"{cloud_root}/{rel_dir}/{name}",
            "name": name,
            "asset_id": str(asset.id),
            "folder_path": f"{cloud_root}/{rel_dir}",
        }

    # ── research_scrape fetch (batch EVIDENCE: real fetch + provenance ledger E7/E10) ──
    @staticmethod
    def _fetch_provenance(project: dict) -> dict:
        """This run's ``_fetch_provenance`` ledger (canonical_url -> fetch record)."""
        ledger = ResearchService._assets_ledger(project)
        prov = ledger.setdefault(_FETCH_PROVENANCE_KEY, {})
        if not isinstance(prov, dict):
            prov = ledger[_FETCH_PROVENANCE_KEY] = {}
        return prov

    @staticmethod
    def _source_id(cu: str) -> str:
        """Deterministic Source node id for a canonical URL (E1 identity key)."""
        digest = hashlib.sha256(cu.encode("utf-8")).hexdigest()[:16]
        return f"{_SOURCE_ID_PREFIX}{digest}"

    @staticmethod
    def _evidence_id(claim_id: str, cu: str) -> str:
        """Deterministic Evidence node id for one (Claim, canonical URL) pair (E1)."""
        digest = hashlib.sha256(f"{claim_id}\x00{cu}".encode()).hexdigest()[:16]
        return f"{_EVIDENCE_ID_PREFIX}{digest}"

    @staticmethod
    def _scrape_body(full: str) -> str:
        """The file's article body (everything after the leading ``-->`` marker)."""
        marker = "\n-->\n"
        idx = full.find(marker)
        return full[idx + len(marker):] if idx != -1 else full

    @staticmethod
    def _fetch_view(o: dict, *, saved: bool, reason: str | None = None, **extra: Any) -> dict:
        """The model-facing shape of one fetch result (never carries the full ``body``)."""
        view = {
            "url": o.get("url", ""),
            "canonical_url": o.get("canonical_url", ""),
            "status": o.get("status", "error"),
            "content_status": o.get("content_status", "n/a"),
            "saved": bool(saved),
            "title": o.get("title", ""),
            "http_status": o.get("http_status"),
            "final_url": o.get("final_url", ""),
            "full_char_len": o.get("full_char_len", 0),
            "char_len": o.get("char_len", 0),
            "text": o.get("text", ""),
            "truncated": bool(o.get("truncated", False)),
        }
        if o.get("status") == "error":
            view["error"] = o.get("error")
        for key, value in extra.items():
            if value:
                view[key] = value
        if reason:
            view["reason"] = reason
        return view

    async def fetch_save_batch(
        self,
        owner_id: uuid.UUID,
        project_id: str,
        *,
        urls: list[str],
        transport=None,
        resolver=None,
    ) -> list[dict]:
        """Fetch ≤3 URLs concurrently, persist usable drafts under temp/vN/scrape, and record
        server-side fetch provenance (E7) for this run.

        Contract (the E-series trust boundaries):
        - **E3** a code-level batch cap: more than 3 URLs is a ``ValueError``.
        - **E7** every successful fetch records ``_fetch_provenance[canonical_url]`` in the
          run's ``cloud_assets`` ledger — ``verify`` later trusts only this record, never a
          model-supplied ``content_status``/length.
        - **E10** saves run concurrently under ``asyncio.gather``; a failed drive write marks
          that URL unsaved and is **not** entered into the ledger (no account skew). It never
          fails the batch.
        - **P3-2A CAS**: each URL is looked up in the global fetch cache first. A HIT skips
          only the network — the draft is still persisted as a fresh *current-run* asset and
          gets a full ledger entry (``cache_hit=True`` + original ``fetched_at`` +
          ``retrieved_at``), so E6/E7/E10 behave exactly as on a MISS. Eligible MISSes
          (usable, no secret-bearing query on request or final URL) are published to the
          cache best-effort after a successful fetch.
        - **P3-2B single-flight**: concurrent MISS fetches of the same cache identity in
          this process/event loop collapse to one network call (exclusive lease +
          generation-fenced takeover inside ``fetch_cache.FlightRegistry``); waiters get
          the shared envelope but keep their full per-run persist/ledger discipline.
        - Silent by contract (like ``save_scrape``): no run event / monitor wake-up per batch.

        Returns one model-facing view per input URL (order preserved; ``body`` stripped).
        """
        raw = [u for u in (urls or []) if isinstance(u, str) and u.strip()]
        if len(raw) > 3:
            raise ValueError(
                f"fetch_save_batch accepts at most 3 URLs per call (got {len(raw)}); "
                "fetch in batches of ≤3"
            )
        if not raw:
            return []
        project = self._load_project(owner_id, project_id)
        cloud_root = project.get("cloud_folder_path")
        rv = self._run_version_of(project)
        if not cloud_root or not rv:
            return [
                self._fetch_view(
                    {"url": u, "canonical_url": canonicalize(u), "status": "error",
                     "content_status": "n/a", "title": "", "http_status": None,
                     "final_url": canonicalize(u), "full_char_len": 0, "char_len": 0,
                     "text": "", "truncated": False},
                    saved=False, reason="no versioned cloud task run",
                )
                for u in raw
            ]
        rel_dir = f"temp/v{rv}/{_SCRAPE_SUBDIR}"
        folder_path = f"{cloud_root}/{rel_dir}"
        try:
            await self._ensure_cloud_dir(owner_id, project, rel_dir)
        except Exception as exc:  # noqa: BLE001 - best-effort, never fail the turn
            logger.warning("research fetch dir ensure failed for %s: %s", project_id, exc)
            return [
                self._fetch_view(
                    {"url": u, "canonical_url": canonicalize(u), "status": "error",
                     "content_status": "n/a", "title": "", "http_status": None,
                     "final_url": canonicalize(u), "full_char_len": 0, "char_len": 0,
                     "text": "", "truncated": False},
                    saved=False, reason="scrape folder unavailable",
                )
                for u in raw
            ]
        # ── P3-2A pre-fetch CAS lookup (public drafts only) ──
        # The cache is a strict accelerator: any store/lookup fault degrades to a miss and
        # costs one re-fetch, never data. A HIT never skips the run-scoped bookkeeping:
        # the draft is still materialized as a *current-run* drive asset and ledger entry
        # below (cache ≠ provenance), so read_fetch/E6/E7/E10 discipline is identical.
        store = FetchStore(self.scratch_root / "fetch_store")
        n_call = len({u.strip() for u in raw})  # E4: whole-batch text budget denominator
        per_url_chars = max(1, DEFAULT_TEXT_TARGET // n_call)
        cu_of = [canonicalize(u) for u in raw]
        hits: dict[int, dict] = {}
        miss_idx: list[int] = []
        for i, cu in enumerate(cu_of):
            hit = store.lookup(cu) if cu else None
            if hit is None:
                miss_idx.append(i)
            else:
                hits[i] = hit
        envelopes: list[dict] = []
        by_index: dict[int, dict] = {}
        for i, meta in hits.items():
            body = meta["body_bytes"].decode("utf-8", errors="replace")
            shown = body[:per_url_chars]
            by_index[i] = {
                "url": raw[i],
                "canonical_url": cu_of[i],
                "status": "ok",
                "title": meta.get("title", ""),
                "http_status": meta.get("http_status"),
                "final_url": meta.get("final_url", cu_of[i]),
                "content_status": meta.get("content_status", "usable"),
                "full_char_len": len(body),
                "char_len": len(shown),
                "text": shown,
                "truncated": len(body) > len(shown),
                "body": body,
            }
        if miss_idx:
            # ── P3-2B single-flight (in-process, event-loop-local) ──
            # MISSes of the same cache identity (flight key == the P3-2A cache_index_id)
            # collapse to ONE real network call: the first caller leads, concurrent ones
            # wait behind the shared future; lease expiry hands leadership to exactly one
            # generation-fenced successor. Only the NETWORK collapses — every consumer
            # below still re-slices at its own batch n, materializes its own current-run
            # asset and writes its own ledger entry (cache ≠ provenance).
            transport_eff = transport or _FETCH_TRANSPORT_OVERRIDE
            resolver_eff = resolver or _FETCH_RESOLVER_OVERRIDE

            async def _net(i: int) -> dict:
                envs = await fetch_clean_urls(
                    [raw[i]], transport=transport_eff, resolver=resolver_eff
                )
                return envs[0] if envs else {
                    "url": raw[i], "canonical_url": cu_of[i], "status": "error",
                    "error": {"type": "transport", "message": "empty fetch result"},
                    "title": "", "http_status": None, "final_url": cu_of[i],
                    "content_status": "n/a", "full_char_len": 0, "char_len": 0,
                    "text": "", "truncated": False,
                }

            results = await asyncio.gather(
                *(
                    _FETCH_FLIGHTS.run(cache_index_id(cu_of[i]), lambda i=i: _net(i))
                    for i in miss_idx
                )
            )
            for i, shared in zip(miss_idx, results):
                o = dict(shared)  # the flight result is SHARED — copy before re-slicing
                if o.get("status") == "ok":
                    # Re-slice the view text at the WHOLE-call n (the flight fetched with
                    # its own n=1 budget); the persisted body is untouched.
                    body = o.get("body") or ""
                    shown = body[:per_url_chars]
                    o["text"] = shown
                    o["char_len"] = len(shown)
                    o["truncated"] = o.get("full_char_len", len(body)) > len(shown)
                by_index[i] = o
        for i in range(len(raw)):
            envelopes.append(
                by_index.get(i)
                or {  # defensive: input dup collapsed by the fetcher's own dedup
                    "url": raw[i], "canonical_url": cu_of[i], "status": "error",
                    "error": {"type": "dedup", "message": "duplicate URL in batch"},
                    "title": "", "http_status": None, "final_url": cu_of[i],
                    "content_status": "n/a", "full_char_len": 0, "char_len": 0,
                    "text": "", "truncated": False,
                }
            )
        # Pre-persist plan for the usable pages (names/content computed offline; no IO yet).
        persist_plan: list[tuple[int, str, bytes]] = []  # (envelope index, name, content bytes)
        plan_of: dict[int, int] = {}  # envelope index -> position in persist_plan
        for i, o in enumerate(envelopes):
            if o.get("status") == "ok" and o.get("content_status") == "usable":
                plan_of[i] = len(persist_plan)
                cu = o.get("canonical_url", "")
                host = cu.split("://", 1)[1].split("/", 1)[0].lower() if "://" in cu else "page"
                host = _safe_filename(host)
                name = f"fetch_{host}_{uuid.uuid4().hex[:10]}.md"
                header = (
                    f"<!-- DeepDive research scrape\n"
                    f"Source: fetch\n"
                    f"URL: {cu}\n"
                    f"Run_version: {rv}\n"
                    f"Retrieved_at: {_now_iso()}\n"
                    f"-->\n\n"
                )
                persist_plan.append((i, name, (header + (o.get("body") or "")).encode("utf-8")))

        async def _persist(name: str, content: bytes) -> dict:
            try:
                asset = await self.drive.save_artifact(
                    owner_id,
                    name=name,
                    mime_type="text/markdown",
                    content=content,
                    folder_path=folder_path,
                    workspace_id=None,
                )
                return {"ok": True, "asset_id": str(asset.id)}
            except Exception as exc:  # noqa: BLE001 - one failed save never kills the batch
                logger.warning("research fetch save failed for %s: %s", name, exc)
                return {"ok": False, "asset_id": None}

        save_results = await asyncio.gather(
            *(_persist(name, content) for _, name, content in persist_plan),
            return_exceptions=True,
        )
        persist_outcomes: dict[int, dict] = {}
        for (idx, _, _), outcome in zip(persist_plan, save_results):
            persist_outcomes[idx] = (
                outcome if isinstance(outcome, dict) else {"ok": False, "asset_id": None}
            )

        # Build the model-facing views + this run's provenance additions.
        additions: dict[str, dict] = {}
        views: list[dict] = []
        for i, o in enumerate(envelopes):
            cu = o.get("canonical_url", "")
            if o.get("status") != "ok":
                views.append(self._fetch_view(o, saved=False))
                continue
            entry: dict[str, Any] = {
                "url": o.get("url", ""),
                "canonical_url": cu,
                "fetch_status": "ok",
                "http_status": o.get("http_status"),
                "content_status": o.get("content_status", "n/a"),
                "full_char_len": o.get("full_char_len", 0),
                "saved": False,
                "asset_id": None,
                "cache_hit": i in hits,  # P3-2A: server-set; a hit still re-materializes
            }
            if o.get("content_status") != "usable":
                additions[cu] = entry  # fetched but unusable → recorded, never verifiable
                views.append(self._fetch_view(o, saved=False))
                continue
            # ── P3-2A cache bookkeeping (fetch succeeded; drive outcome is orthogonal) ──
            # HIT: carry the original fetch time forward — freshness stays anchored to
            # fetched_at, retrieved_at is this run's read stamp only and can never extend
            # the cache entry's life. MISS (eligible): publish to CAS, best-effort.
            if i in hits:
                meta = hits[i]
                entry["content_hash"] = meta.get("sha256")
                entry["fetched_at"] = meta.get("fetched_at_iso")
                entry["retrieved_at"] = _now_iso()
            elif classify_eligibility(cu, o.get("final_url") or cu) == "public":
                body_bytes = (o.get("body") or "").encode("utf-8")
                if store.store(cu, body_bytes, {
                    "title": o.get("title", ""),
                    "http_status": o.get("http_status"),
                    "final_url": o.get("final_url", cu),
                    "content_status": o.get("content_status"),
                    "full_char_len": o.get("full_char_len", 0),
                }):
                    entry["content_hash"] = hashlib.sha256(body_bytes).hexdigest()
                    entry["fetched_at"] = _now_iso()
            outcome = persist_outcomes.get(i) or {"ok": False, "asset_id": None}
            if not outcome.get("ok"):
                # E10: a failed drive write is left OUT of the ledger (no account skew).
                views.append(self._fetch_view(o, saved=False, reason="drive write failed"))
                continue
            asset_id = outcome["asset_id"]
            name = persist_plan[plan_of[i]][1]
            entry.update(
                {
                    "saved": True,
                    "asset_id": asset_id,
                    "name": name,
                    "path": f"{folder_path}/{name}",
                }
            )
            additions[cu] = entry
            views.append(
                self._fetch_view(
                    o, saved=True, asset_id=asset_id, path=entry["path"], name=name,
                    cache_hit=True if i in hits else None,
                )
            )
        if additions:
            self._merge_cloud_assets(
                owner_id, project_id, {_FETCH_PROVENANCE_KEY: additions}
            )
        return views

    async def read_fetch(
        self,
        owner_id: uuid.UUID,
        project_id: str,
        *,
        canonical_url: str | None = None,
        asset_id: str | None = None,
        name: str | None = None,
    ) -> dict:
        """Read back a page full draft this run actually fetched (E6/E9 write-time citation).

        Resolution is locked to this run's ``_fetch_provenance`` ledger: only assets the run
        fetched and saved can be read, addressed by exactly one of ``canonical_url`` /
        ``asset_id`` / ``name``. Anything else — a foreign run's asset, a path with ``..``, a
        made-up id — is refused with a precise error (never a blind drive read).
        """
        project = self._load_project(owner_id, project_id)
        cloud_root = project.get("cloud_folder_path")
        rv = self._run_version_of(project)
        if not cloud_root or not rv:
            raise ValueError(
                "read_fetch needs a versioned cloud task run (no fetch provenance this run)"
            )
        provided = sum(1 for x in (canonical_url, asset_id, name) if x)
        if provided != 1:
            raise ValueError("read_fetch needs exactly one of canonical_url | asset_id | name")
        prov = self._fetch_provenance(project)
        entries = [e for e in prov.values() if isinstance(e, dict) and e.get("saved")]
        if canonical_url:
            cu = canonicalize(canonical_url)
            entry = prov.get(cu) if isinstance(prov, dict) else None
            if not isinstance(entry, dict) or not entry.get("saved"):
                raise ValueError(
                    f"no usable fetch recorded this run for {cu!r} — read only what research_scrape fetch saved"
                )
            asset_id, name = entry["asset_id"], entry.get("name")
        elif asset_id:
            match = next((e for e in entries if e.get("asset_id") == asset_id), None)
            if match is None:
                raise ValueError(
                    f"asset {asset_id!r} was not fetched this run — read only what research_scrape fetch saved"
                )
            asset_id, name = match["asset_id"], match.get("name")
            cu = match["canonical_url"]
        else:  # by name — never a path; a plain filename inside this run's scrape folder.
            if (
                not name
                or name in (".", "..")
                or "/" in name
                or "\\" in name
                or name != _safe_filename(name)
            ):
                raise ValueError(f"invalid scrape file name: {name!r}")
            match = next((e for e in entries if e.get("name") == name), None)
            if match is None:
                raise ValueError(f"no fetch asset named {name!r} this run")
            asset_id, cu = match["asset_id"], match["canonical_url"]
        try:
            full = await self.drive.read_text(owner_id, uuid.UUID(asset_id))
        except Exception as exc:
            logger.warning("research fetch read failed for %s: %s", asset_id, exc)
            raise ValueError(f"could not read fetched asset {asset_id}: drive read failed") from exc
        return {
            "canonical_url": cu,
            "asset_id": asset_id,
            "name": name,
            "path": f"{cloud_root}/temp/v{rv}/{_SCRAPE_SUBDIR}/{name}",
            "content": self._scrape_body(full),
        }

    def ingest_evidence(
        self,
        owner_id: uuid.UUID,
        project_id: str,
        *,
        claim: dict,
        findings: list[dict],
    ) -> dict:
        """Bulk-verify one Claim's findings into the graph (idempotent, provenance-gated).

        The single write path behind ``research_evidence action="verify"``. Trust boundaries:

        - **E8** the Claim must already exist (recorded first with ``record_node``) — verify
          never creates a Claim and never edits an existing Claim's statement/label.
        - **E1** every verified finding maps to Source ``src:<sha(cu)>`` (reused when a Source
          with ``url==cu`` exists) and Evidence ``ev:<sha(claim_id \x00 cu)>`` — replaying the
          same (Claim, URL) is an upsert, never a duplicate node/edge.
        - **E7** a finding is verifiable **only** when this run's ``_fetch_provenance`` ledger
          (written by ``fetch_save_batch``) records the URL as fetched-ok with usable content;
          the model-supplied ``content_status``/length/``usable`` carry no authority.
        - **E2** a Source becomes ``verified`` only when the ledger says
          fetch ok + content usable + ``full_char_len ≥ MIN`` + verdict is
          supports/contradicts. ``neutral`` is not a ticket: it never creates nodes and adds no
          Claim edge — it only annotates an existing verified Evidence for the same (Claim, URL).
        """
        claim_id = (claim or {}).get("id") if isinstance(claim, dict) else None
        if not isinstance(claim_id, str) or not claim_id.strip():
            raise ValueError("verify 'claim' must include the recorded claim's 'id'")
        claim_id = claim_id.strip()
        # P3-3 boundary repair: a JSON-encoded findings list is un-wrapped ONCE before the
        # E7/E1/E2 core runs, then re-enters the unchanged path. A container that is still
        # not a list after the single attempt (bad JSON, scalar, dict) is rejected here with
        # the same wording verify_batch uses for the same field — verify never guesses.
        findings, _findings_coerced = _coerce_json_container(findings, expect="list")
        if findings is not None and not isinstance(findings, list):
            raise ValueError("verify 'findings' must be a list")
        project = self._load_project(owner_id, project_id)
        graph = self._load_graph(owner_id, project_id)
        claim_node = next((n for n in graph["nodes"] if n["id"] == claim_id), None)
        if claim_node is None:
            raise ValueError(
                f"claim node not found: {claim_id!r} — verify does not create claims; "
                "record it first with research_evidence record_node (type 'Claim')"
            )
        claim_label = claim_node.get("label") or claim_id
        prov = self._fetch_provenance(project)
        out = self._apply_findings(graph, claim_id, claim_label, prov, findings)
        if out["changed"]:
            self._save_graph(owner_id, project_id, graph)
        return _with_coerced({
            "claim_id": claim_id,
            "nodes_added": out["nodes_added"],
            "nodes_updated": out["nodes_updated"],
            "edges_added": out["edges_added"],
            "verified_sources": out["verified_sources"],
            "rejected": out["rejected"],
            "neutral_skipped": out["neutral_skipped"],
        }, _findings_coerced, "findings")

    def _apply_findings(
        self,
        graph: dict,
        claim_id: str,
        claim_label: str,
        prov: dict,
        findings: list[dict],
    ) -> dict:
        """The shared E1/E2/E7 ingest core — the SINGLE business path of ``verify`` and
        ``verify_batch`` (P3-1 constraints 7+8: batch ≡ sequential, never a copy).

        Mutates ``graph`` in place and returns
        ``{nodes_added, nodes_updated, edges_added, verified_sources, rejected,
        neutral_skipped, changed}``. Extracted verbatim from :meth:`ingest_evidence`;
        ``changed`` tells the caller whether a graph write is needed at all.
        """
        nodes_by_id = {n["id"]: n for n in graph["nodes"]}
        edges = graph["edges"]

        counts = {"nodes_added": 0, "nodes_updated": 0, "edges_added": 0}
        verified_sources: list[str] = []
        rejected: list[dict] = []
        neutral_skipped: list[dict] = []
        changed = False

        def _add_edge(src: str, dst: str, kind: str) -> bool:
            """Append ``(src, dst, kind)`` if absent; returns True when newly added."""
            if any(e["src"] == src and e["dst"] == dst and e["kind"] == kind for e in edges):
                return False
            edges.append({"src": src, "dst": dst, "kind": kind})
            return True

        for f in findings or []:
            if not isinstance(f, dict):
                rejected.append({"cu": "", "reason": "finding is not an object"})
                continue
            url = (f.get("url") or "").strip()
            verdict = (f.get("verdict") or "").strip().lower()
            if not url:
                rejected.append({"cu": "", "reason": "finding is missing 'url'"})
                continue
            if verdict not in ("supports", "contradicts", "neutral"):
                rejected.append({"cu": url, "reason": f"unknown verdict {verdict!r} (supports|contradicts|neutral)"})
                continue
            cu = canonicalize(url)
            entry = prov.get(cu) if isinstance(prov, dict) else None
            if not isinstance(entry, dict):
                rejected.append(
                    {"cu": cu, "reason": "no fetch provenance this run (fetch the URL with research_scrape fetch first)"}
                )
                continue
            fetch_ok = entry.get("fetch_status") == "ok"
            content_ok = (
                fetch_ok
                and entry.get("content_status") == "usable"
                and int(entry.get("full_char_len", 0)) >= MIN_FETCH_TEXT_CHARS
            )

            if verdict in ("supports", "contradicts"):
                if not content_ok:
                    if entry.get("fetch_status") != "ok":
                        reason = "fetch failed this run (not verifiable)"
                    elif entry.get("content_status") != "usable":
                        reason = (
                            f"fetched content {entry.get('content_status')} (not usable "
                            "for verification — login wall / empty page)"
                        )
                    else:
                        reason = "fetched content too short to verify"
                    rejected.append({"cu": cu, "reason": reason})
                    continue
                # Reuse an existing Source whose url == cu (any id), else the deterministic one.
                source = next(
                    (n for n in graph["nodes"] if n["type"] == "Source" and n.get("url") == cu),
                    None,
                )
                if source is None:
                    source_id = self._source_id(cu)
                    source = nodes_by_id.get(source_id)
                    if source is None:
                        source = {
                            "id": source_id,
                            "type": "Source",
                            "label": (f.get("source_label") or "").strip() or cu,
                            "url": cu,
                            "verification_status": "verified",
                            "canonical_url": cu,
                            "full_char_len": entry.get("full_char_len"),
                            "content_status": entry.get("content_status"),
                            "asset_id": entry.get("asset_id"),
                            "status": "VALID",
                        }
                        graph["nodes"].append(source)
                        nodes_by_id[source_id] = source
                        counts["nodes_added"] += 1
                        changed = True
                    else:
                        source["verification_status"] = "verified"
                else:
                    if source.get("verification_status") != "verified":
                        source["verification_status"] = "verified"
                        counts["nodes_updated"] += 1
                        changed = True
                    source_id = source["id"]

                ev_id = self._evidence_id(claim_id, cu)
                evidence = nodes_by_id.get(ev_id)
                facts = [s for s in (f.get("facts") or []) if isinstance(s, str) and s.strip()]
                excerpt = (f.get("excerpt") or "").strip()
                if evidence is None:
                    evidence = {
                        "id": ev_id,
                        "type": "Evidence",
                        "label": f"{claim_label} → {source.get('label', cu)}",
                        "verdict": verdict,
                        "source_url": cu,
                        "facts": facts,
                        "excerpt": excerpt,
                        "status": "VALID",
                    }
                    graph["nodes"].append(evidence)
                    nodes_by_id[ev_id] = evidence
                    counts["nodes_added"] += 1
                    changed = True
                else:
                    touched = False
                    if evidence.get("verdict") != verdict:
                        evidence["verdict"] = verdict
                        touched = True
                    if (evidence.get("facts") or []) != facts:
                        evidence["facts"] = facts
                        touched = True
                    if (evidence.get("excerpt") or "") != excerpt:
                        evidence["excerpt"] = excerpt
                        touched = True
                    if touched:
                        counts["nodes_updated"] += 1
                        changed = True
                if _add_edge(ev_id, source_id, "depends_on"):
                    counts["edges_added"] += 1
                    changed = True
                if _add_edge(claim_id, ev_id, verdict):
                    counts["edges_added"] += 1
                    changed = True
                verified_sources.append(cu)
            else:  # verdict == neutral — a note on an existing verified Evidence, nothing new.
                source = next(
                    (n for n in graph["nodes"] if n["type"] == "Source" and n.get("url") == cu),
                    None,
                )
                evidence = nodes_by_id.get(self._evidence_id(claim_id, cu))
                linked_ok = source is not None and source.get("verification_status") == "verified"
                if not linked_ok or evidence is None:
                    neutral_skipped.append(
                        {
                            "cu": cu,
                            "reason": "neutral is not a ticket: no verified Evidence for this (claim, url) yet",
                        }
                    )
                    continue
                if evidence.get("verdict") != "neutral":
                    evidence["verdict"] = "neutral"
                    counts["nodes_updated"] += 1
                    changed = True

        return {
            **counts,
            "verified_sources": verified_sources,
            "rejected": rejected,
            "neutral_skipped": neutral_skipped,
            "changed": changed,
        }

    # Upper bound on claims committed per ``verify_batch`` call. Sized to one EVIDENCE
    # stage's claim set (Run 7: 8) — big enough to fold the whole stage into one commit,
    # small enough to keep the critical section and the LLM payload bounded.
    _VERIFY_BATCH_MAX = 8

    def verify_batch(
        self,
        owner_id: uuid.UUID,
        project_id: str,
        *,
        batch: list[dict],
    ) -> dict:
        """Commit a batch of per-claim verifications in ONE atomic transaction (P3-1).

        The deterministic (no-LLM, no-network — constraint 1) vertical slice behind
        ``research_evidence action="verify_batch"``. Business semantics are *inherited*,
        not reimplemented: each pending item runs the exact ``_apply_findings`` core that
        single-claim ``verify`` uses (E1/E2/E7 + error classification, constraint 7), so
        batch ≡ sequential final graph (constraint 8).

        Delta-pending rule (constraint 2, frozen in ``plugins/research/batch.py``):
        an item is skipped iff this run's stored ``_verify_fps`` baseline exists, the
        claim passes the CLAIM_GATE predicate, and its ``evidence_fingerprint`` is
        unchanged. A legacy single-``verify`` anchor (no baseline yet) is therefore
        pending exactly once — the skipping call *stamps* the baseline, a pure
        bookkeeping write, never a re-derivation.

        Citations/strength are accepted as optional per-item patches and written in the
        SAME commit (this is the "补齐 citations" single-write that kills the
        verify→mutate double-anchor chain), subject to the same canonical-vocabulary
        normalization as ``record_node``/``mutate_node``.

        Commit protocol: the whole batch runs inside :meth:`atomic_update_project` with
        ``extra_files=["graph.json"]`` — graph + project.json + revision + ``_verify_fps``
        ledger become visible together or not at all (constraint 5). When every item is
        skipped and no stamp is needed the mutation returns ``False``: zero writes, zero
        revision bump (transaction-level idempotency, constraint 6). Structural
        validation errors reject only the offending item (partial isolation) — the rest
        of the batch still commits. P3-3 boundary repair: a stringified ``findings`` /
        ``citations`` JSON array inside an item is un-wrapped ONCE before those checks
        (audited via a per-item ``coerced`` tag); unparseable or wrong-container inputs
        reject exactly as before.
        """
        if not isinstance(batch, list) or not batch:
            raise ValueError("verify_batch needs 'batch' as a non-empty list of items")
        if len(batch) > self._VERIFY_BATCH_MAX:
            raise ValueError(
                f"verify_batch accepts at most {self._VERIFY_BATCH_MAX} items per call "
                f"(got {len(batch)}); split the claims into batches of ≤{self._VERIFY_BATCH_MAX}"
            )

        meta: dict = {}

        def _mutate(project: dict, extras: dict) -> Any:
            graph = extras.get("graph.json")
            if not isinstance(graph, dict) or not isinstance(graph.get("nodes"), list):
                graph = {"nodes": [], "edges": []}
            extras["graph.json"] = graph  # own the default so the txn writes a valid file
            prov = self._fetch_provenance(project)
            ledger = self._assets_ledger(project)
            stored_fps = ledger.get("_verify_fps")
            if not isinstance(stored_fps, dict):
                stored_fps = {}
            new_fps = dict(stored_fps)  # copy: the ledger changes only on a real commit

            results: list[dict] = []
            seen: set[str] = set()
            dirty = False

            for item in batch:
                if not isinstance(item, dict):
                    results.append({"status": "rejected", "reason": "batch item is not an object"})
                    continue
                item_id = item.get("item_id")
                if not isinstance(item_id, str) or not item_id.strip():
                    results.append({"status": "rejected", "reason": "batch item needs a non-empty 'item_id' string"})
                    continue
                item_id = item_id.strip()
                if item_id in seen:
                    results.append({"item_id": item_id, "status": "rejected", "reason": "duplicate item_id in batch"})
                    continue
                seen.add(item_id)
                claim_id = (item.get("claim") or {}).get("id") if isinstance(item.get("claim"), dict) else None
                if not isinstance(claim_id, str) or not claim_id.strip():
                    results.append({"item_id": item_id, "status": "rejected", "reason": "verify_batch 'claim' must include the recorded claim's 'id'"})
                    continue
                claim_id = claim_id.strip()
                # P3-3 boundary repair (per item, before the unchanged type checks): the
                # ONLY repair attempted is one json.loads of a stringified container that
                # matches the expected type; failures fall into the same rejects below.
                item_coerced: list[str] = []

                def _push(res: dict) -> None:
                    if item_coerced:
                        res["coerced"] = item_coerced
                    results.append(res)

                findings = item.get("findings", [])
                findings, f_coerced = _coerce_json_container(findings, expect="list")
                if f_coerced:
                    item_coerced.append("findings_from_json_string")
                if not isinstance(findings, list):
                    _push({"item_id": item_id, "claim_id": claim_id, "status": "rejected",
                           "reason": "verify_batch 'findings' must be a list"})
                    continue
                citations = item.get("citations")
                citations, c_coerced = _coerce_json_container(citations, expect="list")
                if c_coerced:
                    item_coerced.append("citations_from_json_string")
                if citations is not None and not isinstance(citations, list):
                    _push({"item_id": item_id, "claim_id": claim_id, "status": "rejected",
                           "reason": "verify_batch 'citations' must be a list of strings"})
                    continue
                strength_raw = item.get("strength")
                strength_norm: str | None = None
                if strength_raw is not None:
                    strength_norm = normalize_claim_strength(strength_raw)
                    if strength_norm is None:
                        _push({"item_id": item_id, "claim_id": claim_id, "status": "rejected",
                               "reason": str(_claim_strength_error(strength_raw))})
                        continue

                node = next((n for n in graph["nodes"] if n.get("id") == claim_id), None)
                if node is None:  # E8 — the exact single-verify classification
                    _push({"item_id": item_id, "claim_id": claim_id, "status": "rejected",
                           "reason": f"claim node not found: {claim_id!r} — verify does not create claims; "
                                     "record it first with research_evidence record_node (type 'Claim')"})
                    continue

                current_fp = evidence_fingerprint(graph, claim_id)
                stored = new_fps.get(claim_id)
                gate = _batch_gate_ok(node, normalize_claim_strength)
                if not compute_pending(stored, current_fp, gate):
                    _push({"item_id": item_id, "claim_id": claim_id, "status": "skipped_unchanged",
                           "pending": False, "evidence_fingerprint": current_fp})
                    continue

                out = self._apply_findings(graph, claim_id, node.get("label") or claim_id, prov, findings)
                item_changed = out["changed"]
                if citations is not None:
                    node["citations"] = citations
                    item_changed = True
                if strength_norm is not None:
                    node["strength"] = strength_norm
                    item_changed = True
                if citations is not None or strength_norm is not None:
                    # Same epistemic-delta semantics as mutate_node (constraint 7/8): a
                    # Claim patch STALE-cascades to its dependents through the one
                    # shared _cascade — no parallel reimplementation.
                    if self._cascade(graph, claim_id, to_invalid=False):
                        item_changed = True
                fp_after = evidence_fingerprint(graph, claim_id)
                if item_changed or stored != fp_after:
                    new_fps[claim_id] = fp_after
                    dirty = True
                _push({
                    "item_id": item_id, "claim_id": claim_id, "status": "applied",
                    "pending": True, "changed": item_changed,
                    "nodes_added": out["nodes_added"], "nodes_updated": out["nodes_updated"],
                    "edges_added": out["edges_added"],
                    "verified_sources": out["verified_sources"], "rejected": out["rejected"],
                    "neutral_skipped": out["neutral_skipped"],
                    "evidence_fingerprint": fp_after,
                })

            meta["revision_before"] = project.get("project_revision", 0)
            meta["items"] = results
            if not dirty:
                return False  # all skipped with a live baseline: no file is written at all
            ledger["_verify_fps"] = new_fps
            extras["graph.json"] = graph
            return None

        project = self.atomic_update_project(
            owner_id, project_id, _mutate, extra_files=["graph.json"],
        )
        return {
            "items": meta.get("items", []),
            "revision_before": meta.get("revision_before"),
            "revision_after": project.get("project_revision"),
        }

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
        and one appended row. The record/finish algorithm itself lives in the domain-free
        Workflow Core (``workflow.ledger``); this shell owns loading and persistence.
        """
        self._load_project(owner_id, project_id)
        path = self._project_dir(owner_id, project_id) / "executions.json"
        data = self._load_json(path, {"executions": []})
        row, created = ledger_record_into(
            data,
            execution_id=execution_id,
            tool=tool,
            args=args,
            now_iso=_now_iso(),
            extra_fields={"project_id": project_id},
        )
        if created:
            self._save_json(path, data)
        return {
            "execution_id": row["execution_id"],
            "status": row["status"],
            "idempotent": not created,
        }

    def finish_execution(
        self, owner_id: uuid.UUID, project_id: str, *, execution_id: str, result: Any
    ) -> dict:
        path = self._project_dir(owner_id, project_id) / "executions.json"
        data = self._load_json(path, {"executions": []})
        row = ledger_finish_into(
            data, execution_id=execution_id, result=result, now_iso=_now_iso()
        )
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
    concurrency_safe: bool = False,
) -> Any:
    # P3-1 constraint-1 flag mapping. The frozen loop decides parallelism PER TOOL
    # (``_is_concurrency_safe`` reads the tool attribute; per-ACTION flags are impossible
    # without touching packages/agent/engine/*), so a tool that mixes reads with
    # unlocked read-modify-write commits on graph.json must be mapped conservatively:
    # default False serializes every state-mutating research tool, while the pure
    # network-I/O research_scrape (no scratch graph commit) opts in via ``True``.
    return define_tool(
        name=name,
        description=description,
        parameters=parameters,
        output=ToolOutput(schema={"type": "object"}, render=_render_json),
        execute=handler,
        is_concurrency_safe=concurrency_safe,
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


# The LLM-facing action enum per tool. This is the *visible* action space: it mirrors the
# handler branches the model may drive. ``research_evidence`` deliberately omits
# ``link_edge`` / ``query_lineage`` — edges are written by ``verify`` itself and lineage is
# internal plumbing; their handler branches and service methods stay for internal callers.
_PROJECT_ACTIONS = ["create", "resume", "snapshot", "archive"]
_ARTIFACT_ACTIONS = ["write_scratch", "promote_to_drive", "read", "create_version", "diff"]
_STATE_ACTIONS = ["get_state", "transition_stage", "get_handoff"]
_EVIDENCE_ACTIONS = ["record_node", "mutate_node", "invalidate_downstream", "verify", "verify_batch"]
_GATE_ACTIONS = ["check", "explain_failure", "request_override", "resolve_override"]
_RUN_ACTIONS = ["record_execution", "finish_execution", "execute_sandbox_script"]
_SCRAPE_ACTIONS = ["save_scrape", "fetch", "read"]


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


def _unknown_action(tool: str, action: str, allowed_actions: list[str]) -> ValueError:
    """Structured fallback error for an action that no handler branch implements.

    Schema validation already rejects out-of-enum actions before the handler runs, so this
    is a *defensive* net (e.g. an internal caller bypassing the schema). The payload is
    machine-readable JSON so a model that lands here repairs in one shot instead of
    guessing at free-form prose.
    """
    return ValueError(json.dumps(
        {
            "error": "invalid_action",
            "tool": tool,
            "received": action,
            "allowed_actions": allowed_actions,
        },
        ensure_ascii=False,
    ))


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
                execution_mode=args.get("execution_mode", "strict"),
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
        raise _unknown_action("research_project", action, _PROJECT_ACTIONS)

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
        raise _unknown_action("research_artifact", action, _ARTIFACT_ACTIONS)

    async def _state(args: dict, exec: ToolExecution) -> dict:
        svc = service()
        action = args["action"]
        if action == "get_state":
            return svc.get_state(user(), _project_id(args, "research_state", action))
        if action == "transition_stage":
            return svc.transition_stage(
                user(), _project_id(args, "research_state", action),
                target=_require(args, "target", "research_state", action),
                expected_current_stage=args.get("expected_current_stage"),
            )
        if action == "get_handoff":
            return svc.get_handoff(user(), _project_id(args, "research_state", action))
        raise _unknown_action("research_state", action, _STATE_ACTIONS)

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
        if action == "verify":
            return svc.ingest_evidence(
                user(), _project_id(args, "research_evidence", action),
                claim=_require(args, "claim", "research_evidence", action),
                findings=_require(args, "findings", "research_evidence", action),
            )
        if action == "verify_batch":
            return svc.verify_batch(
                user(), _project_id(args, "research_evidence", action),
                batch=_require(args, "batch", "research_evidence", action),
            )
        raise _unknown_action("research_evidence", action, _EVIDENCE_ACTIONS)

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
        raise _unknown_action("research_gate", action, _GATE_ACTIONS)

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
        raise _unknown_action("research_run", action, _RUN_ACTIONS)

    async def _scrape(args: dict, exec: ToolExecution) -> dict:
        svc = service()
        action = args["action"]
        if action == "save_scrape":
            # Deliberately NOT wrapped in _monitor_wrap: scrape saves are silent (no per-save
            # monitor wake-up or chat line — safety rule 3); the folder shows up on the next
            # natural stage change.
            return await svc.save_scrape(
                user(), _project_id(args, "research_scrape", action),
                source=_require(args, "source", "research_scrape", action) or "source",
                url=args.get("url") or "",
                query=_require(args, "query", "research_scrape", action),
                content=_require(args, "content", "research_scrape", action),
            )
        if action == "fetch":
            # E3 hard code-level cap: >3 URLs is a parameter error, never fetched.
            urls = args.get("urls")
            if not isinstance(urls, list) or not urls or any(not isinstance(u, str) or not u.strip() for u in urls):
                raise ValueError("research_scrape fetch needs 'urls' as a non-empty list of URL strings")
            if len(urls) > 3:
                raise ValueError(
                    f"research_scrape fetch accepts at most 3 URLs per call (got {len(urls)}); "
                    "fetch a claim's sources in batches of ≤3"
                )
            # Silent by contract (like save_scrape): no per-fetch monitor wake-up.
            # F1 output-contract fix: the tool's shared schema is ``{"type": "object"}``
            # (see _make_tool), but fetch_save_batch natively returns one view per URL as
            # a list — wrap it so the batch array passes output validation (order kept).
            return {
                "results": await svc.fetch_save_batch(
                    user(), _project_id(args, "research_scrape", action), urls=urls
                )
            }
        if action == "read":
            return await svc.read_fetch(
                user(), _project_id(args, "research_scrape", action),
                canonical_url=args.get("canonical_url"),
                asset_id=args.get("asset_id"),
                name=args.get("name"),
            )
        raise _unknown_action("research_scrape", action, _SCRAPE_ACTIONS)

    research_project_tool = _make_tool(
        name="research_project",
        description=(
            "ResearchProject container: the tenant-scoped workspace for one Research OS "
            "workflow (state machine in docs/research/07). "
            "Supported actions: create (new project), resume (reopen an existing project), "
            "snapshot (point-in-time copy incl. recorded diagnostics), archive (retire a "
            "project). "
            "Constraints: 'create' requires 'name'; 'execution_mode' ('strict' default | "
            "'progressive') is LOCKED at creation and cannot change afterwards; "
            "resume/snapshot/archive need 'project_id' (auto-bound by the research handoff). "
            "Never create a second project for an existing task — resume it."
        ),
        parameters=_params(
            _PROJECT_ACTIONS,
            {
                "name": {"type": "string", "description": "Project display name."},
                "profile": {
                    "type": "string",
                    "description": "Research profile (Method x Output), e.g. literature.",
                },
                "execution_mode": {
                    "type": "string",
                    "description": "Execution control-flow mode: 'strict' (default) or "
                    "'progressive'. Orthogonal to profile. progressive records gate-FAIL "
                    "diagnostics into project['diagnostics'] and lets the stage advance; "
                    "strict blocks on a failed gate as today.",
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
            "ResearchArtifact IO: scratch-first authoring with explicit drive promotion. "
            "Supported actions: write_scratch (author content in the scratch workspace), "
            "promote_to_drive (publish an artifact to the cloud drive — marks the asset "
            "RAG_PENDING so the projection worker indexes it; promotion is the ONLY path "
            "into the knowledge base), read (fetch a version's content), create_version "
            "(append a new version, e.g. to fix a draft), diff (compare two versions). "
            "Constraints: write_scratch takes the FULL text in ONE call and auto-versions — "
            "do not call it repeatedly for incremental edits; 'artifact_id' is required for "
            "write/promote/read/create_version/diff; 'version' is optional on read (latest "
            "when omitted)."
        ),
        parameters=_params(
            _ARTIFACT_ACTIONS,
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
            "State-machine access: where the project is and where it may legally go next. "
            "Supported actions: get_state (current stage + metadata), get_handoff (the "
            "next-turn briefing: stage, pending gates, open work), transition_stage (advance "
            "to the single legal next stage). "
            "Constraints: call get_handoff BEFORE every transition_stage — it lists what the "
            "next move requires; transition_stage requires 'target' and 'expected_current_stage' "
            "is an optional CAS guard. transition_stage returns one of seven outcomes: "
            "ADVANCED (committed — this ENDS your turn; never re-do the old stage's work), "
            "ALREADY_AT_TARGET (benign no-op), NOT_READY (gate never checked), GATE_BLOCKED "
            "(strict mode, check/repair the gate — never bypass here), CONFLICT (stage moved "
            "concurrently), ILLEGAL (not the single legal next stage), ERROR. "
            "Legal-transition-only: jumps and skips are rejected."
        ),
        parameters=_params(
            _STATE_ACTIONS,
            {
                "target": {"type": "string", "description": "Requested next stage."},
                "expected_current_stage": {
                    "type": "string",
                    "description": "Optional CAS guard: the stage you believe the project is "
                    "on. If it already moved, the transition returns CONFLICT instead of "
                    "acting on stale state.",
                },
            },
            required=[],
        ),
        handler=_monitor_wrap("research_state", _state),
        permission={ToolPermission.READ},
    )

    research_evidence_tool = _make_tool(
        name="research_evidence",
        description=(
            "Evidence-graph writes, batch verification, and staleness cascade. "
            "Supported actions: record_node (insert/update a graph node), mutate_node (patch "
            "a recorded node — mutating an upstream node STALE-cascades to its epistemic "
            "dependents), invalidate_downstream (mark a node's dependents INVALID), verify "
            "(batch-EVIDENCE ingest for ONE claim), verify_batch (the same ingest rules for "
            "UP TO 8 claims committed in ONE atomic transaction — prefer it over one verify "
            "per claim; claims whose stored evidence fingerprint is unchanged and already "
            "anchored return skipped_unchanged with zero re-writing, and each item may "
            "carry optional citations/strength patches written in the SAME commit). "
            "Constraints: there is NO manual edge action — 'verify' writes Source/Evidence "
            "nodes and claim edges itself, idempotently; do not try to link or query lineage "
            "by hand. 'record_node' is idempotent by node['id']; a Claim's 'strength' uses "
            "the canonical vocabulary asserted|supported|confident|contested (report-style "
            "high/medium/low are normalized). 'verify' requires claim{id} of an "
            "already-recorded claim (it never creates or edits the claim) plus "
            "findings[{url, verdict: supports|contradicts|neutral}]; only URLs this run's "
            "server-side fetch ledger (research_scrape fetch) confirms were fetched-ok and "
            "usable become edges — your own content_status/length assertions carry no "
            "authority."
        ),
        parameters=_params(
            _EVIDENCE_ACTIONS,
            {
                "node": {
                    "type": "object",
                    "description": "Node record: {id, type, label?, status?, ...}. Pass the "
                    "OBJECT itself — never pre-encode it as a JSON string.",
                },
                "patch": {
                    "type": "object",
                    "description": "Node mutation fields (e.g. {status: 'INVALID'}).",
                },
                "claim": {
                    "type": "object",
                    "description": "verify: the already-recorded claim node's id, e.g. {id: '...'} "
                    "(verify never creates or edits a claim).",
                },
                "findings": {
                    "type": "array",
                    "description": "verify: [{url, verdict: supports|contradicts|neutral, "
                    "source_label?, facts?: [str], excerpt?: str}]. Pass the ARRAY itself — "
                    "never pre-encode it as a JSON string. The service checks each URL "
                    "against this run's server-side fetch ledger — your content_status/"
                    "length assertions carry no authority.",
                },
                "batch": {
                    "type": "array",
                    "description": "verify_batch: up to 8 items "
                    "[{item_id: str, claim: {id}, findings: [same shape as verify], "
                    "citations?: [str], strength?: asserted|supported|confident|contested}]. "
                    "Per item, 'findings'/'citations' must be native JSON arrays, never "
                    "JSON-encoded strings (a stringified container is repaired once and "
                    "audited via a 'coerced' tag on that item). One atomic commit (single "
                    "revision bump) for the whole batch; a structural error rejects only "
                    "that item.",
                    "items": {"type": "object"},
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
            "Quality gates: deterministic checks and the human-override ladder. "
            "Supported actions: check (evaluate a gate — deterministic, never a judgment "
            "call), explain_failure (item-by-item reasons for a failed gate), "
            "request_override (ask a human to waive a failed strict-mode gate), "
            "resolve_override (the human verdict path). "
            "Constraints: 'check', 'explain_failure' and 'request_override' require "
            "'gate_name'; an override spawns a PENDING ResearchApproval that only a human "
            "resolves — the agent must NEVER self-resolve its own override; always carry a "
            "'reason' for review."
        ),
        parameters=_params(
            _GATE_ACTIONS,
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
            "Execution audit ledger and sandbox dispatch. "
            "Supported actions: record_execution (open an immutable ResearchExecution audit "
            "row for a tool call), finish_execution (close a row with its result), "
            "execute_sandbox_script (run generated code in the sandbox). "
            "Constraints: record/finish complete the provenance trail for substantive "
            "synthesis work; 'execute_sandbox_script' is profile-gated — only 'empirical' / "
            "'mixed' projects run code; a 'literature' project's call returns blocked."
        ),
        parameters=_params(
            _RUN_ACTIONS,
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

    research_scrape_tool = _make_tool(
        name="research_scrape",
        description=(
            "Research source capture. All actions are silent: nothing is announced or "
            "printed per call. "
            "Supported actions: save_scrape (file a retrieved page's raw content under "
            "`temp/vN/scrape/`), fetch (server-side real-fetch of a claim's source URLs — "
            "concurrent, SSRF-guarded, cleaned to core text, recorded in this run's "
            "fetch ledger, snippets returned), read (read back a full draft this run "
            "fetched). "
            "Constraints: save_scrape requires 'source', 'query' and 'content' ('url' "
            "optional); content must be extracted text/markdown, never raw JSON. 'fetch' "
            "takes 'urls' with a HARD cap of 3 per call — more is rejected as a parameter "
            "error, so batch a claim's sources in groups of ≤3; call it per claim THEN "
            "research_evidence verify; a URL whose fetch failed can never be verified. "
            "'read' addresses only pages this run fetched (canonical_url / asset_id / name)."
        ),
        parameters=_params(
            _SCRAPE_ACTIONS,
            {
                "source": {
                    "type": "string",
                    "description": "save_scrape: where the content came from (site/domain or social handle).",
                },
                "url": {
                    "type": "string",
                    "description": "save_scrape: the original URL of the retrieved page.",
                },
                "query": {
                    "type": "string",
                    "description": "save_scrape: the search query this page answered (part of the file name).",
                },
                "content": {
                    "type": "string",
                    "description": "save_scrape: the extracted page content as text/markdown (never raw JSON).",
                },
                "urls": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "fetch: ≤3 URLs to real-fetch this run (SSRF-guarded, "
                    "concurrent, cleaned to core text).",
                },
                "canonical_url": {
                    "type": "string",
                    "description": "read: the canonical URL of a page this run fetched.",
                },
                "asset_id": {
                    "type": "string",
                    "description": "read: the asset id of a page this run fetched.",
                },
                "name": {
                    "type": "string",
                    "description": "read: the scrape file name of a page this run fetched.",
                },
            },
            required=[],
        ),
        handler=_scrape,
        permission={ToolPermission.READ, ToolPermission.WRITE},
        concurrency_safe=True,  # pure fetch/save I/O — no scratch graph RMW (P3-1 flag map)
    )

    return Plugin(
        name="research",
        description="Research OS: research tools over the Project/Artifact/Graph/State/Gate/Scrape/Execution contracts.",
        tools=[
            research_project_tool,
            research_artifact_tool,
            research_state_tool,
            research_evidence_tool,
            research_gate_tool,
            research_run_tool,
            research_scrape_tool,
        ],
        inject=["drive", "research_scratch"],
    )


def register_research_plugins(manager, ctx: Any | None = None) -> None:
    """Mount the research plugin on ``manager`` (used by ``apps/api/deps.py``)."""
    manager.register(build_research_plugin(ctx))
