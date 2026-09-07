"""P3-5/6/7 regression tests: graph-write concurrency + batch fetch + context budget.

Two layers:

* **True two-process** (:mod:`subprocess` workers sharing one scratch dir) — the
  cross-process portalocker path that the API + worker processes actually use. The
  pre-P3-6 naked ``_save_graph`` writers lost updates here; this is the lock.
* **Two-instance in-process** — same ``ResearchService`` code with separate instances
  (the realistic multi-tenant wiring) pinning per-method transaction semantics:
  real writes bump the revision exactly once, idempotent replays cost zero writes,
  the pending-asset buffer folds k merges into the next single commit.

Plus the P3-5 contract surfaces (fetch cap 5, dup-URL naming, source-scoped
``terminal_for_run`` on 403/429, concurrency-safety flags) and the P3-7 surfaces
(run-scoped ``already_fetched`` reference views, bounded read windows).
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from agent import Context, PluginManager, SkillRegistry, ToolRuntime
from core.infrastructure.request_context import set_request_user
from plugins.research.batch import evidence_fingerprint
from plugins.research.plugin import (
    FETCH_BATCH_CHAR_CAP,
    FETCH_MAX_PAGE_CHARS,
    FETCH_MAX_URLS,
    READ_DEFAULT_WINDOW_CHARS,
    ResearchService,
    RevisionConflictError,
    register_research_plugins,
)

import plugins.research.plugin as rplugin

USER = uuid.uuid4()
REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _request_user():
    set_request_user(USER)
    yield
    set_request_user(None)


@pytest.fixture(autouse=True)
def _clean_buffer():
    """The P3-6 fold buffer is process-global keyed by project id — start each test cold."""
    rplugin._PENDING_ASSET_MERGES.clear()
    yield
    rplugin._PENDING_ASSET_MERGES.clear()


@pytest.fixture
def env(tmp_path):
    from tests._drive_fakes import make_drive

    drive = make_drive(tmp_path)
    ctx = Context()
    ctx.provide("drive", drive)
    ctx.provide("research_scratch", tmp_path / "scratch")
    runtime = ToolRuntime()
    manager = PluginManager(runtime, SkillRegistry(), ctx)
    register_research_plugins(manager, ctx)
    return SimpleNamespace(
        ctx=ctx, drive=drive, runtime=runtime, manager=manager, scratch=tmp_path / "scratch"
    )


def _proj_path(env, task_id: str) -> Path:
    return env.scratch / str(USER) / task_id / "project.json"


def _proj(env, task_id: str) -> dict:
    return ResearchService._load_json(_proj_path(env, task_id), None)


def _raw_ledger(env, task_id: str) -> dict:
    p = _proj(env, task_id)
    return ((p.get("driver") or {}).get("cloud_assets")) or {}


def _graph(env, task_id: str) -> dict:
    return ResearchService._load_json(
        env.scratch / str(USER) / task_id / "graph.json", {"nodes": [], "edges": []}
    )


def _svc(env) -> ResearchService:
    return ResearchService(drive=env.drive, scratch_root=env.scratch)


async def _new_task(svc: ResearchService) -> str:
    created = await svc.create_task(USER, title="concurrency")
    return created["task_id"]


# ── fetch network double (mirrors TestBatchEvidence) ─────────────────────────
_PUBLIC = "93.184.216.34"


def _article(fact: str, n: int = 12) -> str:
    paras = "".join(
        f"<p>{fact} sentence {i}: the cleaned article text must comfortably clear "
        "the usable floor so the page counts as a verifiable source.</p>"
        for i in range(n)
    )
    return f"<html><head><title>{fact}</title></head><body><nav>sidebar-menu</nav>{paras}</body></html>"


def _page_handler(request: httpx.Request) -> httpx.Response:
    host = request.url.host
    if host == "good.example":
        return httpx.Response(200, text=_article("unique-fact-0001"))
    if host == "boom.example":
        return httpx.Response(200, text=_article("unique-fact-0002"))
    return httpx.Response(404, text="not found")


def _install_fetch(monkeypatch):
    monkeypatch.setattr(rplugin, "_FETCH_TRANSPORT_OVERRIDE", httpx.MockTransport(_page_handler))
    monkeypatch.setattr(
        rplugin, "_FETCH_RESOLVER_OVERRIDE", lambda host: [_PUBLIC]
    )


# ══════════════════════════════════════════════════════════════════════════════
# P3-6 — true two-process hammer (the naked-_save_graph lost-write regression)
# ══════════════════════════════════════════════════════════════════════════════

_WORKER_SRC = '''
import json, sys, time, uuid
from pathlib import Path
sys.path.insert(0, sys.argv[5])
from plugins.research.plugin import ResearchService, ProjectLockError

scratch = Path(sys.argv[1]); user = uuid.UUID(sys.argv[2]); task = sys.argv[3]
worker = sys.argv[4]; n = int(sys.argv[6]); gate = Path(sys.argv[7])

svc = ResearchService(drive=None, scratch_root=scratch)
while not gate.exists():
    time.sleep(0.02)

written, attempts = 0, 0
for i in range(n):
    for retry in range(5):
        attempts += 1
        try:
            out = svc.record_node(user, task, node={
                "id": f"claim:{worker}:{i}", "type": "Claim",
                "label": f"worker {worker} claim {i}",
            })
            written += 0 if out["idempotent"] else 1
            break
        except ProjectLockError:
            time.sleep(0.1)  # transient contention: back off and retry
p = svc._load_json(scratch / str(user) / task / "project.json", None)
out = scratch / f"{worker}.result.json"
out.write_text(json.dumps({
    "written": written, "attempts": attempts, "revision": p["project_revision"],
}), encoding="utf-8")
'''


class TestTwoProcessGraphHammer:
    def test_concurrent_record_node_loses_no_writes(self, env, tmp_path):
        """Two OS processes hammer ``record_node`` on ONE project.

        Pre-P3-6 each did a blind load→append→``_save_graph``; the loser's stale copy
        clobbered the winner's node. Post-P3-6 everything rides
        ``atomic_update_project(extra_files=["graph.json"])`` under the cross-process
        portalocker: every node must survive and the revision must equal the write
        count (each process serializes on the lock, so its own bumps are strictly
        monotonic).
        """
        import plugins.research.plugin as rp_mod

        svc = _svc(env)
        task_id = asyncio.run(_new_task(svc))

        gate = tmp_path / "gate"
        n_per_worker = 30
        procs = []
        for worker in ("A", "B"):
            procs.append(
                subprocess.Popen(
                    [sys.executable, "-c", _WORKER_SRC, str(env.scratch), str(USER),
                     task_id, worker, str(REPO_ROOT), str(n_per_worker), str(gate)],
                    cwd=str(REPO_ROOT),
                )
            )
        time.sleep(0.4)  # let both workers park on the gate, then release together
        gate.touch()
        deadline = time.time() + 120
        for p in procs:
            p.wait(timeout=max(1.0, deadline - time.time()))
            assert p.returncode == 0, f"worker exited {p.returncode}"

        results = [
            json.loads((env.scratch / f"{w}.result.json").read_text(encoding="utf-8"))
            for w in ("A", "B")
        ]
        graph = _graph(env, task_id)
        ids = [n["id"] for n in graph["nodes"]]
        # no lost writes: both workers' full node sets are present, no duplicates
        assert sorted(ids) == sorted(
            f"claim:{w}:{i}" for w in ("A", "B") for i in range(n_per_worker)
        )
        assert len(ids) == len(set(ids))
        assert all(r["written"] == n_per_worker for r in results)
        # every worker's own last bump is its write count plus whatever it saw of the
        # other worker's commits so far — a late-reading worker can legitimately show a
        # LOWER revision than the total; only the final on-disk value is exact.
        assert all(1 + n_per_worker <= r["revision"] <= 1 + 2 * n_per_worker for r in results)
        # the final on-disk revision exactly equals create + the committed graph writes
        final = _proj(env, task_id)
        assert final["project_revision"] == 1 + sum(r["written"] for r in results)
        assert rp_mod._PENDING_ASSET_MERGES.get(task_id) is None  # buffer fully consumed


# ══════════════════════════════════════════════════════════════════════════════
# P3-6 — transaction semantics per migrated writer (two instances, same repo)
# ══════════════════════════════════════════════════════════════════════════════

class TestWriterTransactions:
    async def test_record_and_link_interleaved_no_lost_nodes(self, env):
        """Two service instances interleaving record_node/link_edge over one project:
        the load happens INSIDE the lock, so a stale in-memory snapshot can never
        clobber the other writer's append."""
        svc_a, svc_b = _svc(env), _svc(env)
        task_id = await _new_task(svc_a)
        for i in range(12):
            await asyncio.gather(
                asyncio.to_thread(svc_a.record_node, USER, task_id,
                                  node={"id": f"ca:{i}", "type": "Claim", "label": "a"}),
                asyncio.to_thread(svc_b.record_node, USER, task_id,
                                  node={"id": f"cb:{i}", "type": "Claim", "label": "b"}),
            )
        graph = _graph(env, task_id)
        assert len(graph["nodes"]) == 24
        assert _proj(env, task_id)["project_revision"] == 1 + 24

    async def test_record_node_idempotent_replay_zero_write(self, env):
        svc = _svc(env)
        task_id = await _new_task(svc)
        out1 = svc.record_node(USER, task_id, node={"id": "c1", "type": "Claim"})
        rev1 = _proj(env, task_id)["project_revision"]
        out2 = svc.record_node(USER, task_id, node={"id": "c1", "type": "Claim", "label": "x"})
        assert out1["idempotent"] is False and out2["idempotent"] is True
        assert out2["node"] == out1["node"]  # existing record wins — no silent patch
        assert _proj(env, task_id)["project_revision"] == rev1  # zero writes, zero bump

    async def test_link_edge_dedup_replay_zero_write_and_endpoint_guard(self, env):
        svc = _svc(env)
        task_id = await _new_task(svc)
        svc.record_node(USER, task_id, node={"id": "c1", "type": "Claim"})
        svc.record_node(USER, task_id, node={"id": "c2", "type": "Claim"})
        rev = _proj(env, task_id)["project_revision"]
        e1 = svc.link_edge(USER, task_id, src="c1", dst="c2", kind="supports")
        assert e1["idempotent"] is False
        assert _proj(env, task_id)["project_revision"] == rev + 1
        e2 = svc.link_edge(USER, task_id, src="c1", dst="c2", kind="supports")
        assert e2["idempotent"] is True
        assert _proj(env, task_id)["project_revision"] == rev + 1  # replay: zero bump
        with pytest.raises(ValueError, match="endpoints"):
            svc.link_edge(USER, task_id, src="c1", dst="ghost", kind="supports")
        assert _proj(env, task_id)["project_revision"] == rev + 1  # raise released lock, no write

    async def test_mutate_and_invalidate_commit_atomically(self, env):
        svc = _svc(env)
        task_id = await _new_task(svc)
        for nid in ("root", "mid", "leaf"):
            svc.record_node(USER, task_id, node={"id": nid, "type": "Claim"})
        svc.link_edge(USER, task_id, src="root", dst="mid", kind="supports")
        svc.link_edge(USER, task_id, src="mid", dst="leaf", kind="supports")
        rev_before = _proj(env, task_id)["project_revision"]
        out = svc.invalidate_downstream(USER, task_id, node_id="root")
        graph = _graph(env, task_id)
        statuses = {n["id"]: n["status"] for n in graph["nodes"]}
        assert statuses["root"] == "INVALID"
        assert "mid" in out["cascade"] and statuses["mid"] == "INVALID"
        assert _proj(env, task_id)["project_revision"] == rev_before + 1  # ONE bump

    async def test_stale_snapshot_never_clobbers_committed_graph(self, env):
        # The precise pre-P3-6 lost-write shape: a writer holding an old graph copy
        # must not be able to regress the committed node set — record_node re-reads
        # the graph inside the lock, so the stale copy is simply never consulted.
        svc_a, svc_b = _svc(env), _svc(env)
        task_id = await _new_task(svc_a)
        old_graph = _graph(env, task_id)  # svc-style snapshot a naked writer might hold
        svc_b.record_node(USER, task_id, node={"id": "fresh", "type": "Claim"})
        svc_a.record_node(USER, task_id, node={"id": "late", "type": "Claim"})
        assert old_graph["nodes"] == []  # proof the snapshot WAS stale
        assert {"fresh", "late"} <= {n["id"] for n in _graph(env, task_id)["nodes"]}


# ══════════════════════════════════════════════════════════════════════════════
# P3-6 — the pending-asset buffer: k merges fold into ONE commit
# ══════════════════════════════════════════════════════════════════════════════

class TestPendingAssetBuffer:
    async def test_k_merges_fold_into_one_flush(self, env):
        svc = _svc(env)
        task_id = await _new_task(svc)
        rev0 = _proj(env, task_id)["project_revision"]
        for i in range(3):
            svc._merge_cloud_assets(USER, task_id, {f"k{i}": {"v": i}})
        # buffered, not committed: zero disk writes so far…
        assert _proj(env, task_id)["project_revision"] == rev0
        assert _raw_ledger(env, task_id) == {}
        # …but read paths see them (same-turn read-after-write, E7 hazard)
        merged = ResearchService._assets_ledger(
            ResearchService._overlay_pending(_proj(env, task_id))
        )
        assert {k: merged[k]["v"] for k in ("k0", "k1", "k2")} == {"k0": 0, "k1": 1, "k2": 2}
        # the next real commit carries ALL of them — exactly one revision bump
        svc.record_node(USER, task_id, node={"id": "c1", "type": "Claim"})
        assert _proj(env, task_id)["project_revision"] == rev0 + 1
        ledger = _raw_ledger(env, task_id)
        assert [ledger[k]["v"] for k in ("k0", "k1", "k2")] == [0, 1, 2]
        assert "c1" in {n["id"] for n in _graph(env, task_id)["nodes"]}
        assert task_id not in rplugin._PENDING_ASSET_MERGES  # consumed by the commit

    async def test_noop_replay_does_not_consume_buffer(self, env):
        svc = _svc(env)
        task_id = await _new_task(svc)
        svc._merge_cloud_assets(USER, task_id, {"a": 1})
        # idempotent record_node replay returns False → zero writes → buffer SURVIVES
        svc.record_node(USER, task_id, node={"id": "c1", "type": "Claim"})
        assert _raw_ledger(env, task_id).get("a") == 1
        assert task_id not in rplugin._PENDING_ASSET_MERGES
        svc._merge_cloud_assets(USER, task_id, {"b": 2})
        svc.record_node(USER, task_id, node={"id": "c1", "type": "Claim"})  # replay
        assert task_id in rplugin._PENDING_ASSET_MERGES
        assert _raw_ledger(env, task_id).get("a") == 1  # from the earlier real commit
        assert "b" not in _raw_ledger(env, task_id)  # not consumed by the no-op

    async def test_cas_conflict_keeps_buffer(self, env):
        svc = _svc(env)
        task_id = await _new_task(svc)
        rev0 = _proj(env, task_id)["project_revision"]
        svc._merge_cloud_assets(USER, task_id, {"a": 1})
        with pytest.raises(RevisionConflictError):
            svc.atomic_update_project(
                USER, task_id, lambda p: p.update(x=1), expected_revision=rev0 + 99
            )
        assert task_id in rplugin._PENDING_ASSET_MERGES  # buffer rides the NEXT commit
        svc.record_node(USER, task_id, node={"id": "c1", "type": "Claim"})
        assert _raw_ledger(env, task_id).get("a") == 1
        assert task_id not in rplugin._PENDING_ASSET_MERGES

    async def test_explicit_flush_seam(self, env):
        svc = _svc(env)
        task_id = await _new_task(svc)
        svc._merge_cloud_assets(USER, task_id, {"a": 1})
        assert _raw_ledger(env, task_id) == {}
        svc.flush_asset_merges(USER, task_id)
        assert _raw_ledger(env, task_id).get("a") == 1
        assert task_id not in rplugin._PENDING_ASSET_MERGES
        svc.flush_asset_merges(USER, task_id)  # cold buffer: no-op, no extra bump


# ══════════════════════════════════════════════════════════════════════════════
# P3-5 — fetch batch cap, duplicate naming, budget invariant, safe flags
# ══════════════════════════════════════════════════════════════════════════════

class TestP35FetchContract:
    def test_constants_and_invariant(self):
        # n × max_chars ≤ 150k holds BY CONSTRUCTION at the cap, and the E4 model-facing
        # slice still dominates per-page budget below it.
        assert FETCH_MAX_URLS == 5
        assert FETCH_BATCH_CHAR_CAP == FETCH_MAX_URLS * FETCH_MAX_PAGE_CHARS == 150_000
        for n in range(1, FETCH_MAX_URLS + 1):
            per_url = max(1, min(6000 // n, FETCH_MAX_PAGE_CHARS, FETCH_BATCH_CHAR_CAP // n))
            assert n * per_url <= FETCH_BATCH_CHAR_CAP

    async def test_fetch_accepts_five_and_rejects_six(self, env, monkeypatch):
        svc = _svc(env)
        task_id = await _new_task(svc)
        svc.begin_run(USER, task_id)
        _install_fetch(monkeypatch)
        urls = [f"https://good.example/p{i}" for i in range(FETCH_MAX_URLS)]
        views = await svc.fetch_save_batch(USER, task_id, urls=urls)
        assert len(views) == 5 and all(v["saved"] for v in views)
        with pytest.raises(ValueError, match="at most 5 URLs"):
            await svc.fetch_save_batch(
                USER, task_id, urls=urls + ["https://good.example/p9"]
            )

    async def test_duplicate_canonical_url_names_the_offender(self, env):
        svc = _svc(env)
        task_id = await _new_task(svc)
        with pytest.raises(ValueError) as exc:
            await svc.fetch_save_batch(
                USER, task_id,
                # fragment is stripped by canonicalization — these two are ONE URL
                urls=["https://good.example/x#section", "https://good.example/x"],
            )
        assert "https://good.example/x" in str(exc.value)  # canonical form is named

    def test_concurrency_safe_flags(self):
        from agent import Context as _C, ToolRuntime as _RT
        from apps.api.tools import web_search_tool
        from plugins.social_search.plugin import PLUGIN as social_plugin

        rt = _RT()
        web_search_tool.register(rt, _C(), llm=None)
        assert rt._tools["web_search"].is_concurrency_safe is True
        social = social_plugin.tools[0]
        assert social.name == "search_social" and social.is_concurrency_safe is True


# ══════════════════════════════════════════════════════════════════════════════
# P3-7A — run-scoped Page-Pool reference views (memory-level, no schema)
# ══════════════════════════════════════════════════════════════════════════════

class TestP37AReferenceDedup:
    async def test_same_url_refetched_returns_ref_view_without_text_or_network(
        self, env, monkeypatch
    ):
        svc = _svc(env)
        task_id = await _new_task(svc)
        svc.begin_run(USER, task_id)
        calls: list[str] = []
        base = _page_handler

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return base(request)

        monkeypatch.setattr(rplugin, "_FETCH_TRANSPORT_OVERRIDE", httpx.MockTransport(handler))
        monkeypatch.setattr(
            rplugin, "_FETCH_RESOLVER_OVERRIDE", lambda host: [_PUBLIC]
        )
        cu = "https://good.example/recipe"
        (v1,) = await svc.fetch_save_batch(USER, task_id, urls=[cu])
        assert v1["saved"] and v1["text"] and "already_fetched" not in v1
        (v2,) = await svc.fetch_save_batch(USER, task_id, urls=[cu])
        assert len(calls) == 1  # dedup short-circuits BEFORE any CAS/network work
        assert v2["already_fetched"] is True
        assert v2["text"] == "" and v2["char_len"] == 0
        assert v2["asset_id"] == v1["asset_id"]  # ids survive — the model can reuse them
        assert v2["full_char_len"] == v1["full_char_len"]
        assert "already fetched" in v2["hint"]

    async def test_mixed_batch_preserves_input_order(self, env, monkeypatch):
        svc = _svc(env)
        task_id = await _new_task(svc)
        svc.begin_run(USER, task_id)
        _install_fetch(monkeypatch)
        first = "https://good.example/recipe"
        fresh = "https://boom.example/other"
        await svc.fetch_save_batch(USER, task_id, urls=[first])
        views = await svc.fetch_save_batch(USER, task_id, urls=[first, fresh])
        assert [v["canonical_url"] for v in views] == [first, fresh]  # order restored
        assert views[0]["already_fetched"] is True
        assert views[1]["saved"] is True and views[1]["text"]

    async def test_dedup_works_through_the_unflushed_buffer(self, env, monkeypatch):
        # Same-turn fold (P3-6): the provenance ledger is still BUFFERED when the next
        # fetch runs — the overlayed read must still see it and dedup (E7 hazard lock).
        svc = _svc(env)
        task_id = await _new_task(svc)
        svc.begin_run(USER, task_id)
        _install_fetch(monkeypatch)
        cu = "https://good.example/recipe"
        await svc.fetch_save_batch(USER, task_id, urls=[cu])
        assert task_id in rplugin._PENDING_ASSET_MERGES  # not yet committed to disk
        assert "_fetch_provenance" not in _raw_ledger(env, task_id)
        (v2,) = await svc.fetch_save_batch(USER, task_id, urls=[cu])
        assert v2["already_fetched"] is True

    async def test_ref_view_is_model_safe_shape(self, env, monkeypatch):
        # No new DB entities/schema: the dedup is a pure view-level contract.
        svc = _svc(env)
        task_id = await _new_task(svc)
        svc.begin_run(USER, task_id)
        _install_fetch(monkeypatch)
        cu = "https://good.example/recipe"
        await svc.fetch_save_batch(USER, task_id, urls=[cu])
        proj_dir = env.scratch / str(USER) / task_id
        before = {f.name for f in proj_dir.glob("*.json")}
        (v,) = await svc.fetch_save_batch(USER, task_id, urls=[cu])
        assert set(v) == {
            "url", "canonical_url", "status", "content_status", "saved", "asset_id",
            "name", "path", "full_char_len", "char_len", "text", "truncated",
            "already_fetched", "hint",
        }
        # the ref path creates no new project-dir files — memory-level only, no schema
        assert {f.name for f in proj_dir.glob("*.json")} == before


# ══════════════════════════════════════════════════════════════════════════════
# P3-7B — telemetry-derived bounded read window
# ══════════════════════════════════════════════════════════════════════════════

class TestP37BReadWindow:
    async def test_read_window_params_and_metadata(self, env, monkeypatch):
        svc = _svc(env)
        task_id = await _new_task(svc)
        svc.begin_run(USER, task_id)
        _install_fetch(monkeypatch)
        cu = "https://good.example/recipe"
        view = await svc.fetch_save_batch(USER, task_id, urls=[cu])
        stored = await env.drive.read_text(USER, uuid.UUID(view[0]["asset_id"]))
        expected = ResearchService._scrape_body(stored)
        total = len(expected)
        full = await svc.read_fetch(USER, task_id, canonical_url=cu)
        assert 0 < total <= READ_DEFAULT_WINDOW_CHARS  # draft ceiling dominates the window
        assert full["content"] == expected
        assert full["total_chars"] == total
        assert full["offset"] == 0 and full["truncated"] is False

        w = await svc.read_fetch(USER, task_id, canonical_url=cu, offset=5, max_chars=40)
        assert w["offset"] == 5 and w["total_chars"] == total
        assert w["content"] == expected[5:45]
        assert w["truncated"] is (45 < total)

        tail = await svc.read_fetch(
            USER, task_id, canonical_url=cu, offset=max(0, total - 10), max_chars=1000
        )
        assert tail["truncated"] is False
        assert tail["content"] == expected[total - 10:]

    def test_window_constant_matches_derivation_bound(self):
        # READ_DEFAULT_WINDOW_CHARS is pinned to the persisted-draft ceiling
        # (DEFAULT_MAX_CHARS=4000 in the fetch layer) so the window can never clip a
        # draft today while bounding unbounded growth later.
        from core.infrastructure.web_fetch import DEFAULT_MAX_CHARS

        assert READ_DEFAULT_WINDOW_CHARS == DEFAULT_MAX_CHARS


# ══════════════════════════════════════════════════════════════════════════════
# P3-5b — search_social 403/429 as source-scoped terminal (unit-level)
# ══════════════════════════════════════════════════════════════════════════════

class TestP35bTerminalForRun:
    async def test_auto_mode_marker_via_cause_chain(self, monkeypatch):
        import plugins.social_search.plugin as social

        async def _boom(query, limit, subreddit):
            try:
                resp = httpx.Response(
                    403, request=httpx.Request("GET", "https://oauth.reddit.com/search")
                )
                raise httpx.HTTPStatusError("denied", request=resp.request, response=resp)
            except httpx.HTTPStatusError as exc:
                raise RuntimeError("wrapped") from exc

        monkeypatch.setitem(social._ADAPTERS, "reddit", _boom)
        merged = await social._execute({"query": "q", "platform": "auto"}, exec=None)
        markers = [m for m in merged if m.get("terminal_for_run")]
        assert len(markers) == 1 and markers[0]["platform"] == "reddit"
        assert markers[0]["http_status"] == 403

    async def test_transient_error_still_raises(self, monkeypatch):
        import os
        from unittest import mock

        import plugins.social_search.plugin as social

        async def _boom(query, limit, subreddit):
            resp = httpx.Response(
                500, request=httpx.Request("GET", "https://www.reddit.com/search.json")
            )
            raise httpx.HTTPStatusError("boom", request=resp.request, response=resp)

        # Deterministic regardless of the machine's real reddit OAuth env: force the
        # explicit-platform path (which maps errors) by patching the adapter table.
        with mock.patch.dict(
            os.environ,
            {"REDDIT_CLIENT_ID": "", "REDDIT_CLIENT_SECRET": "",
             "REDDIT_USERNAME": "", "REDDIT_PASSWORD": ""},
            clear=False,
        ):
            monkeypatch.setitem(social._ADAPTERS, "reddit", _boom)
            with pytest.raises(RuntimeError, match="HTTP 500"):
                await social._execute({"query": "q", "platform": "reddit"}, exec=None)


# ══════════════════════════════════════════════════════════════════════════════
# _verify_fps never ahead of the graph (P3-6 concurrency invariant)
# ══════════════════════════════════════════════════════════════════════════════

class TestFpsNeverAheadOfGraph:
    async def test_interleaved_verify_batch_final_state_consistent(self, env, monkeypatch):
        svc_a, svc_b = _svc(env), _svc(env)
        task_id = await _new_task(svc_a)
        svc_a.begin_run(USER, task_id)  # before fetch: begin_run resets the run ledger
        _install_fetch(monkeypatch)
        await svc_a.fetch_save_batch(USER, task_id, urls=["https://good.example/recipe"])
        cu = "https://good.example/recipe"
        for i in range(6):
            (svc_a if i % 2 == 0 else svc_b).record_node(
                USER, task_id, node={"id": f"cl{i}", "type": "Claim", "label": f"fact {i}"}
            )
        for i in range(6):
            svc = svc_a if i % 2 == 0 else svc_b
            out = svc.verify_batch(USER, task_id, batch=[{
                "item_id": f"i{i}", "claim": {"id": f"cl{i}"},
                "findings": [{"url": cu, "verdict": "supports", "facts": [f"f{i}"]}],
                "citations": [cu], "strength": "supported",
            }])
            assert out["items"][0]["status"] == "applied"
        svc.flush_asset_merges(USER, task_id)  # force the buffered ledgers to disk

        graph = _graph(env, task_id)
        ledger = _raw_ledger(env, task_id)
        fps = ledger.get("_verify_fps") or {}
        node_ids = {n["id"] for n in graph["nodes"]}
        # never ahead: every stamped claim exists in the committed graph…
        assert set(fps) <= node_ids
        # …and every stamp equals the fingerprint recomputed over the FINAL graph
        for claim_id, stored in fps.items():
            assert stored == evidence_fingerprint(graph, claim_id)
        # all six claims carry their Evidence tickets — nothing was lost
        for i in range(6):
            assert evidence_fingerprint(graph, f"cl{i}") != evidence_fingerprint(
                {"nodes": [], "edges": []}, f"cl{i}"
            )
