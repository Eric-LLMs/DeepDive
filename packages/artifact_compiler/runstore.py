"""RunStore: the canonical state authority for compiler runs (docs/research/19 §6).

Transaction discipline is the same one ``ResearchService.atomic_update_project``
proved in production:

* portalocker exclusive lock (``LOCK_EX | LOCK_NB`` retried until timeout — a blocking
  portalocker lock ignores its timeout) serializes API/worker/CLI processes;
* inside the critical section the state is re-read fresh and CAS-verified against
  ``expected_revision`` when supplied, mutated, stamped with a monotonic
  ``run_revision`` and persisted durably (``.tmp`` -> ``fsync`` -> ``os.replace``);
* multi-file commits stage every payload before *any* replace, and the
  revision-bearing ``run.json`` is replaced LAST — a half-applied commit can never
  present mutated content under an un-bumped revision;
* ``_replace_durable`` absorbs the Windows/drvfs sharing-violation window.

Only JSON state goes through this class; byte artifacts (``report.typ``, ``.svg``,
``.pdf``) are *derived* and written by the engines as plain files. Every mutation
entrypoint demands a :class:`PrincipalContext` and refuses foreign owners
(invariant 9).
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import portalocker
from pydantic import BaseModel

from artifact_compiler.states import (
    PUBLISHABLE_STATES,
    TERMINALS,
    RunState,
    validate_transition,
)

LOCK_TIMEOUT_S = 30.0
RUN_FILE = "run.json"
_LOCK_NAME = ".run.lock"


class RunNotFound(ValueError):
    pass


class RevisionConflictError(RuntimeError):
    """CAS failure: on-disk ``run_revision`` != the caller's expected value."""


class PrincipalMismatchError(PermissionError):
    """A PrincipalContext tried to mutate a run owned by a different principal."""


class RunIdError(ValueError):
    """run_id failed the path-safety guard (separator / traversal / charset)."""


@dataclass(frozen=True)
class PrincipalContext:
    """Who is driving the engine. ``owner_id`` mirrors the research tenancy key
    (UUID string); ``project_id`` is optional (pure-CLI runs have none yet)."""

    owner_id: str
    project_id: str | None = None

    @staticmethod
    def from_uuid(owner_id: uuid.UUID, project_id: str | None = None) -> PrincipalContext:
        return PrincipalContext(owner_id=str(owner_id), project_id=project_id)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _to_jsonable(value: Any) -> Any:
    """Recursively convert BaseModels (incl. inside lists/dicts) to JSON types."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_to_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _to_jsonable(v) for k, v in value.items()}
    return value


def _check_run_id(run_id: str) -> str:
    if (
        not run_id
        or len(run_id) > 128
        or os.sep in run_id
        or "/" in run_id
        or ".." in run_id
        or not all(c.isalnum() or c in "-_." for c in run_id)
    ):
        raise RunIdError(f"unsafe run_id: {run_id!r}")
    return run_id


class RunStore:
    """File-backed canonical store rooted at a caller-supplied directory
    (``data/artifact_runs`` in the app; ``tmp_path`` in tests; standalone CLI chooses
    its own — the Core itself reads no app settings)."""

    def __init__(self, root: Path, *, lock_timeout: float = LOCK_TIMEOUT_S) -> None:
        self.root = Path(root)
        self.lock_timeout = lock_timeout

    # ── paths & primitives ────────────────────────────────────────────────────

    def run_dir(self, run_id: str) -> Path:
        return self.root / _check_run_id(run_id)

    def _state_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / RUN_FILE

    @staticmethod
    def _dump_tmp(path: Path, data: Any) -> Path:
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            fh.write(json.dumps(data, indent=2, ensure_ascii=False, default=str))
            fh.flush()
            os.fsync(fh.fileno())
        return tmp

    @staticmethod
    def _replace_durable(src: Path, dst: Path) -> None:
        """``os.replace`` that survives the transient Windows/drvfs sharing violation."""
        for attempt in range(6):
            try:
                os.replace(src, dst)
                return
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.02 * (2**attempt))

    def _load(self, path: Path, default: Any = None) -> Any:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except FileNotFoundError:
            return default

    def _locked(self, run_id: str):
        dir_path = self.run_dir(run_id)
        dir_path.mkdir(parents=True, exist_ok=True)
        return portalocker.Lock(
            str(dir_path / _LOCK_NAME),
            timeout=self.lock_timeout,
            check_interval=0.05,
            flags=portalocker.LOCK_EX | portalocker.LOCK_NB,
        )

    def _require_owner(self, state: dict, principal: PrincipalContext) -> None:
        if state.get("owner_id") != principal.owner_id:
            raise PrincipalMismatchError(
                f"run {state.get('run_id')} belongs to another principal"
            )

    # ── lifecycle API ─────────────────────────────────────────────────────────

    def create_run(
        self,
        principal: PrincipalContext,
        *,
        run_id: str | None = None,
        artifact_id: str | None = None,
        budget: dict | None = None,
        metadata: dict | None = None,
    ) -> dict:
        """Mint a QUEUED run record (idempotency is the caller's job: an existing
        run_id raises). Returns the persisted state."""
        run_id = _check_run_id(run_id) if run_id is not None else f"ar-{uuid.uuid4().hex[:16]}"
        path = self._state_path(run_id)
        with self._locked(run_id):
            if path.exists():
                raise ValueError(f"run already exists: {run_id}")
            state = {
                "run_id": run_id,
                "artifact_id": artifact_id or run_id,
                "owner_id": principal.owner_id,
                "project_id": principal.project_id,
                "state": RunState.QUEUED.value,
                "run_revision": 1,
                "repair_attempts": 0,
                "created_at": _now_iso(),
                "updated_at": _now_iso(),
                "budget": budget or {},
                "metadata": metadata or {},
                "documents": {},  # name -> {sha256, revision}
                "history": [{"state": RunState.QUEUED.value, "at": _now_iso()}],
            }
            self._replace_durable(self._dump_tmp(path, state), path)
        return state

    def get_run(self, run_id: str) -> dict:
        state = self._load(self._state_path(run_id))
        if state is None:
            raise RunNotFound(f"run not found: {run_id}")
        return state

    def transition(
        self,
        principal: PrincipalContext,
        run_id: str,
        to_state: RunState,
        *,
        expected_revision: int | None = None,
        note: str | None = None,
    ) -> dict:
        """CAS-bumped lifecycle hop. Terminal escape and stage skipping raise
        :class:`IllegalTransition` without touching disk."""
        if not isinstance(to_state, RunState):
            to_state = RunState(to_state)
        return self._mutate(
            principal, run_id, expected_revision=expected_revision,
            mutate=lambda s: self._apply_transition(s, to_state, note),
        )

    @staticmethod
    def _apply_transition(state: dict, to_state: RunState, note: str | None) -> None:
        current = RunState(state["state"])
        validate_transition(current, to_state)
        state["state"] = to_state.value
        if note:
            state["last_note"] = note
        state.setdefault("history", []).append({"state": to_state.value, "at": _now_iso()})

    def _mutate(
        self,
        principal: PrincipalContext,
        run_id: str,
        *,
        expected_revision: int | None,
        mutate: Callable[[dict], None],
    ) -> dict:
        """One critical section: fresh load -> owner check -> CAS -> mutation ->
        durable commit of ``run.json`` (``.tmp`` -> fsync -> replace)."""
        path = self._state_path(run_id)
        with self._locked(run_id):
            state = self._load(path)
            if state is None:
                raise RunNotFound(f"run not found: {run_id}")
            self._require_owner(state, principal)
            if (
                expected_revision is not None
                and state.get("run_revision", 0) != expected_revision
            ):
                raise RevisionConflictError(
                    f"run {run_id} revision changed: expected {expected_revision}, "
                    f"got {state.get('run_revision', 0)}"
                )
            mutate(state)
            state["run_revision"] = int(state.get("run_revision", 0)) + 1
            state["updated_at"] = _now_iso()
            self._replace_durable(self._dump_tmp(path, state), path)
        return state

    # ── canonical document commits ───────────────────────────────────────────

    def put_document(
        self,
        principal: PrincipalContext,
        run_id: str,
        name: str,
        payload: Any,
        *,
        expected_revision: int | None = None,
    ) -> dict:
        """Atomically (re)write a canonical ``<run>/<name>.json`` and bump the run
        revision in the same transaction. ``payload``: BaseModel | dict."""
        if "/" in name or "\\" in name or ".." in name or not name.strip():
            raise ValueError(f"unsafe document name: {name!r}")
        if not name.endswith(".json"):
            name = f"{name}.json"
        obj = _to_jsonable(payload)
        blob = json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=True)
        sha = hashlib.sha256(blob.encode("utf-8")).hexdigest()
        doc_path = self.run_dir(run_id) / name

        def mutate(state: dict) -> None:
            state.setdefault("documents", {})[name] = {
                "sha256": sha,
                "revision": int(state.get("run_revision", 0)) + 1,
                "at": _now_iso(),
            }

        # stage the document bytes before the lock-guarded commit of run.json:
        # extras-first ordering via extra_files would duplicate the dump logic, so
        # the doc is written inside the same critical section instead.
        with self._locked(run_id):
            path = self._state_path(run_id)
            state = self._load(path)
            if state is None:
                raise RunNotFound(f"run not found: {run_id}")
            self._require_owner(state, principal)
            if (
                expected_revision is not None
                and state.get("run_revision", 0) != expected_revision
            ):
                raise RevisionConflictError(
                    f"run {run_id} revision changed: expected {expected_revision}, "
                    f"got {state.get('run_revision', 0)}"
                )
            mutate(state)
            self._replace_durable(self._dump_tmp(doc_path, obj), doc_path)
            state["run_revision"] = int(state.get("run_revision", 0)) + 1
            state["updated_at"] = _now_iso()
            self._replace_durable(self._dump_tmp(path, state), path)
        return state

    def get_document(self, run_id: str, name: str) -> Any:
        if not name.endswith(".json"):
            name = f"{name}.json"
        if "/" in name or "\\" in name or ".." in name:
            raise ValueError(f"unsafe document name: {name!r}")
        doc = self._load(self.run_dir(run_id) / name)
        if doc is None:
            raise RunNotFound(f"document {name!r} missing for run {run_id}")
        return doc

    # ── convenience helpers used by later phases ─────────────────────────────

    def bump_repair_attempts(
        self, principal: PrincipalContext, run_id: str
    ) -> int:
        new_attempts = 0

        def mutate(state: dict) -> None:
            nonlocal new_attempts
            state["repair_attempts"] = int(state.get("repair_attempts", 0)) + 1
            new_attempts = state["repair_attempts"]

        self._mutate(principal, run_id, expected_revision=None, mutate=mutate)
        return new_attempts

    @staticmethod
    def is_publishable(state: dict) -> bool:
        return RunState(state["state"]) in PUBLISHABLE_STATES

    @staticmethod
    def is_terminal(state: dict) -> bool:
        return RunState(state["state"]) in TERMINALS
