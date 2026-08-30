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

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from agent.engine.decisions import ToolExecution, text_block
from agent.plugins.base import Plugin
from agent.tools.definition import ToolOutput, define_tool
from agent.tools.tool_permissions import ToolPermission
from core.infrastructure.request_context import get_request_user_id

# ── stage / gate vocabulary (frozen in docs/research/07 + 10) ─────────────────
_STAGES = [
    "INBOX",
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
    "INBOX": "DISCOVER",
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
    def _project_dir(self, owner_id: uuid.UUID, project_id: str) -> Path:
        return self.scratch_root / str(owner_id) / project_id

    @staticmethod
    def _load_json(path: Path, default: Any) -> Any:
        if not path.is_file():
            return default
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _save_json(path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
        )
        os.replace(tmp, path)

    def _load_project(self, owner_id: uuid.UUID, project_id: str) -> dict:
        path = self._project_dir(owner_id, project_id) / "project.json"
        project = self._load_json(path, None)
        if project is None:
            raise ValueError(f"project not found: {project_id}")
        return project

    def _save_project(self, project: dict) -> None:
        project["updated_at"] = _now_iso()
        path = self._project_dir(uuid.UUID(project["owner_id"]), project["id"]) / "project.json"
        self._save_json(path, project)

    def _load_graph(self, owner_id: uuid.UUID, project_id: str) -> dict:
        path = self._project_dir(owner_id, project_id) / "graph.json"
        return self._load_json(path, {"nodes": [], "edges": []})

    def _save_graph(self, owner_id: uuid.UUID, project_id: str, graph: dict) -> None:
        self._save_json(self._project_dir(owner_id, project_id) / "graph.json", graph)

    def _artifact_dir(self, owner_id: uuid.UUID, project_id: str, artifact_id: str) -> Path:
        return self._project_dir(owner_id, project_id) / "artifacts" / artifact_id

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
            "stage": "INBOX",
            "gates": {gate: "NOT_RUN" for gate in _GATES},
            "idempotency_key": idempotency_key,
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
        }
        self._save_project(project)
        self._save_graph(owner_id, project["id"], {"nodes": [], "edges": []})
        return {
            "project_id": project["id"],
            "name": name,
            "owner_id": str(owner_id),
            "stage": "INBOX",
            "status": "ACTIVE",
            "profile": profile,
            "idempotent": False,
        }

    def _find_by_idempotency(
        self, owner_id: uuid.UUID, kind: str, idempotency_key: str
    ) -> dict | None:
        owner_dir = self.scratch_root / str(owner_id)
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
        project = self._load_project(owner_id, project_id)
        project["status"] = "ARCHIVED"
        self._save_project(project)
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
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
        }
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
        self, owner_id: uuid.UUID, project_id: str, *, artifact_id: str
    ) -> dict:
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
        record["updated_at"] = _now_iso()
        self._save_json(version_paths[-1], record)
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
        artifact_dir = self._artifact_dir(owner_id, project_id, artifact_id)
        next_version = 1
        if artifact_dir.is_dir():
            next_version = max((int(p.name[1:]) for p in artifact_dir.glob("v*")), default=0) + 1
        previous = self._artifact(owner_id, project_id, artifact_id, next_version - 1)
        record = dict(previous)
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
        project["stage"] = target
        self._save_project(project)
        return {"requested": target, "granted": True, "stage": target, "gate": gate}

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
        graph = self._load_graph(owner_id, project_id)
        node_id = node["id"]
        node_type = node["type"]
        for existing in graph["nodes"]:
            if existing["id"] == node_id:
                raise ValueError(f"node already exists: {node_id}")
        record = {
            "id": node_id,
            "type": node_type,
            "label": node.get("label", node_id),
            "status": node.get("status", "VALID"),
            **{k: v for k, v in node.items() if k not in ("id", "type", "label", "status")},
        }
        graph["nodes"].append(record)
        self._save_graph(owner_id, project_id, graph)
        return {"node": record}

    def link_edge(
        self, owner_id: uuid.UUID, project_id: str, *, src: str, dst: str, kind: str
    ) -> dict:
        graph = self._load_graph(owner_id, project_id)
        ids = {n["id"] for n in graph["nodes"]}
        if src not in ids or dst not in ids:
            raise ValueError(f"edge endpoints must be recorded nodes: {src} -> {dst}")
        edge = {"src": src, "dst": dst, "kind": kind}
        graph["edges"].append(edge)
        self._save_graph(owner_id, project_id, graph)
        return {"edge": edge}

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
        return [
            {
                "name": "sources_verified",
                "ok": sources_ok,
                "detail": (
                    "at least one Source exists and every Source is verified"
                    if sources_ok
                    else "need >=1 Source with verification_status='verified'"
                ),
            },
            {
                "name": "evidence_linked",
                "ok": bool(evidences) and all(linked_to_verified(e["id"]) for e in evidences),
                "detail": "every Evidence node links to a verified Source",
            },
            {
                "name": "no_invalid_upstream",
                "ok": not upstream_invalid,
                "detail": "no INVALID upstream evidence/claim/source",
            },
            {
                "name": "claim_draft_links",
                "ok": bool(claims) and all(linked_to_evidence(c["id"]) for c in claims),
                "detail": ">=1 draft Claim linked to an Evidence node",
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
        project["gates"][gate_name] = status
        self._save_project(project)
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
        project = self._load_project(owner_id, project_id)
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
        self, owner_id: uuid.UUID, approval_id: str, *, approve: bool
    ) -> dict:
        # Locate the approval across the owner's projects.
        approval = None
        project = None
        owner_dir = self.scratch_root / str(owner_id)
        if owner_dir.is_dir():
            for project_dir in owner_dir.iterdir():
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
            project["gates"][approval["gate_name"]] = "OVERRIDE"
            self._save_project(project)
        return {
            "approval_id": approval_id,
            "status": approval["status"],
            "gate_name": approval["gate_name"],
            "approver_user_id": str(owner_id),
            "resolved_at": approval["resolved_at"],
        }

    # ── research_run ──────────────────────────────────────────────────────
    def record_execution(
        self, owner_id: uuid.UUID, project_id: str, *, tool: str, args: dict
    ) -> dict:
        self._load_project(owner_id, project_id)
        path = self._project_dir(owner_id, project_id) / "executions.json"
        data = self._load_json(path, {"executions": []})
        execution = {
            "execution_id": str(uuid.uuid4()),
            "project_id": project_id,
            "tool": tool,
            "args": args,
            "status": "RUNNING",
            "result": None,
            "created_at": _now_iso(),
        }
        data["executions"].append(execution)
        self._save_json(path, data)
        return {"execution_id": execution["execution_id"], "status": "RUNNING"}

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


_ACTIONS = {
    "action": {
        "type": "string",
        "description": "Which action to run on this tool.",
    }
}

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


def _params(extra: dict, required: list[str]) -> dict:
    props = {**_ACTIONS, **_COMMON_OBJ, **extra}
    return {"type": "object", "properties": props, "required": ["action", *required]}


def build_research_plugin(ctx: Any | None = None) -> Plugin:
    """Build the research plugin, capturing ``ctx`` for lazy capability resolution.

    Tools do **not** resolve ``drive``/``research_scratch`` at build time — they call
    ``_service_for(ctx)`` at execute time, so the plugin can be registered (and stay a PENDING
    fiber) before the API provides its capabilities. This mirrors the toolkit factory pattern
    and keeps ``discover()`` compatibility (no module-level ``PLUGIN``).
    """

    def service() -> ResearchService:
        if ctx is None:
            raise RuntimeError("research plugin was built without a Context")
        return ResearchService(
            drive=ctx.resolve("drive"),
            scratch_root=ctx.resolve("research_scratch"),
        )

    def user() -> uuid.UUID:
        return _current_user()

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
        if action == "resume":
            return svc.resume_project(user(), args["project_id"])
        if action == "snapshot":
            return svc.snapshot_project(user(), args["project_id"])
        if action == "archive":
            return svc.archive_project(user(), args["project_id"])
        raise ValueError(f"unknown research_project action: {action}")

    async def _artifact(args: dict, exec: ToolExecution) -> dict:
        svc = service()
        action = args["action"]
        if action == "write_scratch":
            return await svc.write_scratch(
                user(),
                args["project_id"],
                artifact_id=args["artifact_id"],
                content=args["content"],
                idempotency_key=args.get("idempotency_key"),
                generated_by_execution=args.get("generated_by_execution"),
            )
        if action == "promote_to_drive":
            return await svc.promote_to_drive(user(), args["project_id"], artifact_id=args["artifact_id"])
        if action == "read":
            return svc.read_artifact(
                user(), args["project_id"], artifact_id=args["artifact_id"],
                version=args.get("version"),
            )
        if action == "create_version":
            return await svc.create_version(
                user(),
                args["project_id"],
                artifact_id=args["artifact_id"],
                content=args["content"],
                idempotency_key=args.get("idempotency_key"),
            )
        if action == "diff":
            return svc.diff_artifact(
                user(), args["project_id"], artifact_id=args["artifact_id"],
                from_version=args["from_version"], to_version=args["to_version"],
            )
        raise ValueError(f"unknown research_artifact action: {action}")

    async def _state(args: dict, exec: ToolExecution) -> dict:
        svc = service()
        action = args["action"]
        if action == "get_state":
            return svc.get_state(user(), args["project_id"])
        if action == "transition_stage":
            return svc.transition_stage(user(), args["project_id"], target=args["target"])
        if action == "get_handoff":
            return svc.get_handoff(user(), args["project_id"])
        raise ValueError(f"unknown research_state action: {action}")

    async def _evidence(args: dict, exec: ToolExecution) -> dict:
        svc = service()
        action = args["action"]
        if action == "record_node":
            return svc.record_node(user(), args["project_id"], node=args["node"])
        if action == "link_edge":
            return svc.link_edge(user(), args["project_id"], src=args["src"], dst=args["dst"], kind=args["kind"])
        if action == "query_lineage":
            return svc.query_lineage(user(), args["project_id"], node_id=args["node_id"])
        if action == "mutate_node":
            return svc.mutate_node(user(), args["project_id"], node_id=args["node_id"], patch=args["patch"])
        if action == "invalidate_downstream":
            return svc.invalidate_downstream(user(), args["project_id"], node_id=args["node_id"])
        raise ValueError(f"unknown research_evidence action: {action}")

    async def _gate(args: dict, exec: ToolExecution) -> dict:
        svc = service()
        action = args["action"]
        if action == "check":
            return svc.check_gate(user(), args["project_id"], gate_name=args["gate_name"])
        if action == "explain_failure":
            return svc.explain_failure(user(), args["project_id"], gate_name=args["gate_name"])
        if action == "request_override":
            return svc.request_override(user(), args["project_id"], gate_name=args["gate_name"], reason=args["reason"])
        if action == "resolve_override":
            return svc.resolve_override(user(), args["approval_id"], approve=args.get("approve", True))
        raise ValueError(f"unknown research_gate action: {action}")

    async def _run(args: dict, exec: ToolExecution) -> dict:
        svc = service()
        action = args["action"]
        if action == "record_execution":
            return svc.record_execution(user(), args["project_id"], tool=args["tool"], args=args.get("args", {}))
        if action == "finish_execution":
            return svc.finish_execution(user(), args["project_id"], execution_id=args["execution_id"], result=args.get("result"))
        if action == "execute_sandbox_script":
            return svc.execute_sandbox_script(user(), args["project_id"], script=args["script"])
        raise ValueError(f"unknown research_run action: {action}")

    research_project_tool = _make_tool(
        name="research_project",
        description=(
            "Create / resume / snapshot / archive a ResearchProject. Projects are the "
            "tenant-scoped container for a research OS workflow (state machine in "
            "docs/research/07)."
        ),
        parameters=_params(
            {
                "name": {"type": "string", "description": "Project display name."},
                "profile": {
                    "type": "string",
                    "description": "Research profile (Method x Output), e.g. literature.",
                },
            },
            required=[],
        ),
        handler=_project,
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
        handler=_artifact,
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
            {"target": {"type": "string", "description": "Requested next stage."}},
            required=[],
        ),
        handler=_state,
        permission={ToolPermission.READ},
    )

    research_evidence_tool = _make_tool(
        name="research_evidence",
        description=(
            "Record graph nodes and edges, query lineage, and invalidate downstream nodes. "
            "Mutating an upstream node STALE-cascades to its epistemic dependents."
        ),
        parameters=_params(
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
        handler=_evidence,
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
        handler=_gate,
        permission={ToolPermission.READ, ToolPermission.WRITE},
    )

    research_run_tool = _make_tool(
        name="research_run",
        description=(
            "Record (and finish) an immutable ResearchExecution audit row, or execute a "
            "sandbox script. Sandbox execution is profile-gated; the literature profile blocks it."
        ),
        parameters=_params(
            {
                "tool": {"type": "string", "description": "Tool name being audited."},
                "args": {"type": "object", "description": "Execution arguments."},
                "execution_id": {"type": "string", "description": "Execution id to finish."},
                "result": {"type": "object", "description": "Execution result payload."},
                "script": {"type": "string", "description": "Sandbox script body."},
            },
            required=[],
        ),
        handler=_run,
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
