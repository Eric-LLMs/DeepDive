"""Batch-3 handler validation: REVIEW / REPRODUCE / PUBLISH (sealed-spec hard gates).

Doctrine pinned here:

* REVIEW — thinking OFF, tri-state verdict, NEVER a forged pass:
  1. clean patch            -> ``pass`` (no new version);
  2. patch applied          -> ``pass(after_fix)`` (vN+1 via create_version);
  3. rejected patch / invalid twice -> ``review_incomplete``: a
     ``degraded_decision`` ledger line, status ``unreviewed``, HONEST advance
     (the published edition is unreviewed — a quality gap, not a structural
     absence). The physical lock (target + expected_old exact count == 1)
     discards the whole staging area: the draft stays byte-identical.
  The node's ENTIRE LLM throughput is the service closure — it rides
  ``rplugin._ADJ_LLM_CALL`` (NOT the pipeline decision seam), gated by the
  stage's 2-call ceiling; the format-repair pass sees the original prompt +
  the parse error ONLY (no context expansion, no re-retrieval).
* REPRODUCE — pure Python integrity audit, structurally 0 LLM: both seams are
  rigged to RAISE, so any completion attempt fails the test at the source.
* PUBLISH — the absolute terminal hard gate, 0 LLM: legitimate promotable
  substance -> PROMOTED (+drive_asset_id) and the chain terminalizes; anything
  less -> StructuralStop -> BLOCKED. An empty hand never passes.
"""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

import plugins.research.pipeline as pipeline
import plugins.research.plugin as rplugin
from core.infrastructure.request_context import set_request_user
from plugins.research.pipeline import CONTRACTS as PIPE_CONTRACTS

import plugins.research.handlers  # noqa: F401 — side effect: registers all 10 nodes

from agent import Context, PluginManager, SkillRegistry, ToolRuntime
from plugins.research.plugin import ResearchService, register_research_plugins

USER = uuid.uuid4()

DRAFT = (
    "# Tomato Study\n\n"
    "## Section A\n"
    "Cooking raises lycopene levels significantly.\n\n"
    "## Section B\n"
    "Raw lycopene intake absorbs poorly.\n"
)


@pytest.fixture(autouse=True)
def _request_user():
    set_request_user(USER)
    yield
    set_request_user(None)


@pytest.fixture(autouse=True)
def _no_fence():
    from plugins.research.plugin import get_auto_run_fence
    assert get_auto_run_fence() is None
    yield


@pytest.fixture(autouse=True)
def _clean_buffers():
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
    return SimpleNamespace(ctx=ctx, drive=drive, scratch=tmp_path / "scratch")


def _boom_pipeline_seam(monkeypatch):
    """0-LLM nodes: any PIPELINE decision completion is a test failure."""
    async def boom(prompt: str, system: str) -> str:
        raise AssertionError("0-LLM stage touched PIPELINE_LLM_CALL")

    monkeypatch.setattr(pipeline, "PIPELINE_LLM_CALL", boom)


def _boom_adj_seam(monkeypatch):
    """0-LLM nodes: any service-closure completion is a test failure."""
    async def boom(prompt: str, system_prompt: str) -> str:
        raise AssertionError("0-LLM stage touched _ADJ_LLM_CALL")

    monkeypatch.setattr(rplugin, "_ADJ_LLM_CALL", boom)


def _seam(monkeypatch, replies):
    """REVIEW rides the SERVICE closure: patch _ADJ_LLM_CALL, record prompts."""
    seen: list[str] = []

    async def fake(prompt: str, system_prompt: str) -> str:
        seen.append(prompt)
        return replies[min(len(seen) - 1, len(replies) - 1)]

    monkeypatch.setattr(rplugin, "_ADJ_LLM_CALL", fake)
    return seen


async def _at(env, stage: str, *, with_report: bool = True, question: str | None = None,
              seed: dict | None = None):
    """Task parked at `stage` with the upstream deliverables that stage implies."""
    svc = ResearchService(drive=env.drive, scratch_root=env.scratch)
    task = (await svc.create_task(
        USER, title="tomato research", execution_mode="progressive",
    ))["task_id"]
    rid = svc.begin_run(USER, task)["run_id"]
    base = svc.get_driver_checkpoint(USER, task)

    def _seed(p):
        p["driver"] = {**base, "run_id": rid, "execution_id": f"{rid}:1:1"}
        p["stage"] = stage
        if question is not None:
            p["research_question"] = question
        if seed:
            p.setdefault("pipeline", {}).update(seed)
    svc.atomic_update_project(USER, task, _seed)
    svc.record_node(USER, task, node={
        "id": "k1", "type": "Claim", "label": "cooking raises lycopene",
        "statement": "cooking raises lycopene", "strength": "medium",
    })
    if with_report:
        await svc.write_scratch(USER, task, artifact_id="report.md", content=DRAFT)
    return svc, task, rid


def _run(svc, task, rid):
    return pipeline.run_node(
        svc, USER, task, run_id=rid, execution_id=f"{rid}:1:1", turn_index=1,
    )


def _budget(svc, task):
    return svc.read_project(USER, task)["pipeline"]["last_node"]["budget"]


def _proj(svc, task):
    return svc.read_project(USER, task)


def _v1(svc, task):
    return svc._load_json(
        svc._artifact_dir(USER, task, "report.md") / "v1", None,
    )


def _change(old: str, new: str) -> dict:
    return {"file": "report.md", "target": "", "expected_old": old, "change": new}


# ═════════════════════════════ REVIEW — tri-state + physical lock ═════════════

async def test_review_clean_patch_passes_without_new_version(env, monkeypatch):
    assert PIPE_CONTRACTS["REVIEW"].thinking is False       # thinking OFF node
    svc, task, rid = await _at(env, "REVIEW")
    seen = _seam(monkeypatch, ['{"changes": []}'])
    out = await _run(svc, task, rid)
    assert out.kind == "advanced" and out.next_stage == "REPRODUCE"
    assert len(seen) == 1 and _budget(svc, task)["calls"] == 1   # HARD METRIC
    rv = _proj(svc, task)["pipeline"]["review"]
    assert rv["status"] == "pass"
    assert rv["verdict"] == "pass: no corrections required"
    assert rv["new_version"] is None and rv["changes_applied"] == 0
    assert rv["base_version"] == 1
    art = svc.read_artifact(USER, task, artifact_id="report.md")
    assert art["version"] == 1                              # no v2: nothing to fix
    assert out.ledger == []


async def test_review_valid_patch_lands_fix_version(env, monkeypatch):
    svc, task, rid = await _at(env, "REVIEW")
    reply = json.dumps({"changes": [_change("significantly", "by 35% in pooled trials")]})
    seen = _seam(monkeypatch, [reply])
    out = await _run(svc, task, rid)
    assert out.kind == "advanced" and out.next_stage == "REPRODUCE"
    assert len(seen) == 1 and _budget(svc, task)["calls"] == 1   # repair untouched
    rv = _proj(svc, task)["pipeline"]["review"]
    assert rv["status"] == "pass_after_fix"
    assert rv["verdict"] == "pass(after_fix): v2"
    assert rv["new_version"] == 2 and rv["changes_applied"] == 1
    art = svc.read_artifact(USER, task, artifact_id="report.md", version=2)
    assert art["version"] == 2 and "by 35% in pooled trials" in art["content"]
    v1 = svc.read_artifact(USER, task, artifact_id="report.md", version=1)
    assert v1["content"] == DRAFT                             # the draft base stays intact


async def test_review_invalid_twice_degrades_unreviewed_and_repairs_format_only(env, monkeypatch):
    svc, task, rid = await _at(env, "REVIEW")
    seen = _seam(monkeypatch, ["not json at all", "still not json"])
    out = await _run(svc, task, rid)
    # TRI-STATE #3: honest unreviewed — ledger + advance, NEVER a fake pass,
    # NEVER a block (an unreviewed edition is a quality gap, not an absence).
    assert out.kind == "advanced" and out.next_stage == "REPRODUCE"
    assert len(seen) == 2 and _budget(svc, task)["calls"] == 2   # NEVER a third
    assert out.ledger[-1]["error_class"] == "degraded_decision"
    assert "review_incomplete" in out.ledger[-1]["detail"]
    rv = _proj(svc, task)["pipeline"]["review"]
    assert rv["status"] == "unreviewed" and rv["verdict"] == "review_incomplete"
    # the published edition stays UNREVIEWED — the ledger says so out loud
    assert "UNREVIEWED" in out.turn_value
    # LLM#2 is FORMAT-repair only: same input + the parse error, nothing else.
    assert seen[1].startswith(seen[0])
    assert "failed to parse" in seen[1]
    assert "VIOLATIONS" not in seen[1]                        # no decision-repair copy
    art = svc.read_artifact(USER, task, artifact_id="report.md")
    assert art["version"] == 1 and art["content"] == DRAFT    # byte-identical


async def test_review_lock_rejects_multi_match_patch_draft_untouched(env, monkeypatch):
    # expected_old "Raw" appears 0 times... use a 2-match span instead:
    # "lycopene" appears twice in the draft -> count != 1 -> whole patch discarded.
    svc, task, rid = await _at(env, "REVIEW")
    before = _v1(svc, task)
    reply = json.dumps({"changes": [_change("lycopene", "CAROTENOID")]})
    seen = _seam(monkeypatch, [reply])
    out = await _run(svc, task, rid)
    assert len(seen) == 1 and _budget(svc, task)["calls"] == 1   # lock is Python-side
    assert out.kind == "advanced" and out.next_stage == "REPRODUCE"
    assert out.ledger[-1]["error_class"] == "degraded_decision"
    assert "review_incomplete" in out.ledger[-1]["detail"]
    rv = _proj(svc, task)["pipeline"]["review"]
    assert rv["status"] == "unreviewed" and rv["new_version"] is None
    after = _v1(svc, task)
    assert after["content"] == before["content"] == DRAFT        # iron rule: untouched
    assert not (svc._artifact_dir(USER, task, "report.md") / "v2").exists()


async def test_review_lock_rejects_zero_match_patch(env, monkeypatch):
    svc, task, rid = await _at(env, "REVIEW")
    # expected_old "definitely absent span" matches 0 times -> iron rule rejects.
    reply = json.dumps({"changes": [_change("definitely absent span", "x")]})
    seen = _seam(monkeypatch, [reply])
    out = await _run(svc, task, rid)
    assert len(seen) == 1
    assert out.kind == "advanced"
    assert _proj(svc, task)["pipeline"]["review"]["status"] == "unreviewed"
    assert "review_incomplete" in out.ledger[-1]["detail"]


async def test_review_hides_extra_completion_behind_the_gate(env, monkeypatch):
    # A chatty reviewer that WOULD make a 3rd call gets power-cut by the stage
    # gate BEFORE the seam sees it: the failure is an honest ledger line.
    svc, task, rid = await _at(env, "REVIEW")
    calls: list[str] = []

    async def chatty(prompt: str, system_prompt: str) -> str:
        calls.append(prompt)
        return "not json"            # both gated calls unparseable -> RuntimeError

    monkeypatch.setattr(rplugin, "_ADJ_LLM_CALL", chatty)
    # rig review_draft internals to attempt a THIRD completion: emulate by
    # driving the gate directly after the node — the ceiling proof.
    out = await _run(svc, task, rid)
    assert len(calls) == 2 and _budget(svc, task)["calls"] == 2   # ceiling holds
    from plugins.research.llm_budget import RunBudget, StageBudgetExceeded, StageGate
    run = RunBudget(cap_usd=None)
    gate = StageGate(run, stage="REVIEW", max_calls=PIPE_CONTRACTS["REVIEW"].llm_calls)
    gate.admit(); gate.admit()
    with pytest.raises(StageBudgetExceeded):
        gate.admit()               # a 3rd is structurally impossible
    assert out.ledger[-1]["error_class"] == "degraded_decision"


async def test_review_without_report_is_structural_zero_llm(env, monkeypatch):
    svc, task, rid = await _at(env, "REVIEW", with_report=False)
    _boom_pipeline_seam(monkeypatch)
    _boom_adj_seam(monkeypatch)
    out = await _run(svc, task, rid)
    assert out.kind == "blocked"
    assert out.structural["stage"] == "REVIEW"
    assert out.structural["missing"] == "report"
    assert _proj(svc, task)["stage"] == "REVIEW"              # never advanced
    assert _budget(svc, task)["calls"] == 0                   # 0 LLM gate


# ═════════════════════════════ REPRODUCE — pure Python, 0 LLM ════════════════

async def _reproduce_env(env, *, review_seed=True):
    seed = {}
    if review_seed:
        seed["review"] = {"status": "pass", "verdict": "pass: no corrections required",
                          "artifact": "report.md", "base_version": 1}
    return await _at(env, "REPRODUCE", seed=seed)


async def test_reproduce_all_green_zero_llm(env, monkeypatch):
    assert PIPE_CONTRACTS["REPRODUCE"].llm_calls == 0         # declared 0
    svc, task, rid = await _reproduce_env(env)
    _boom_pipeline_seam(monkeypatch)
    _boom_adj_seam(monkeypatch)
    out = await _run(svc, task, rid)
    assert out.kind == "advanced" and out.next_stage == "PUBLISH"
    assert _budget(svc, task)["calls"] == 0                   # HARD METRIC: 0 LLM
    rp = _proj(svc, task)["pipeline"]["reproduce"]
    assert all(c["ok"] for c in rp["checks"]), rp["checks"]
    assert {c["name"] for c in rp["checks"]} == {
        "primary_report_bound", "report_version_on_disk", "report_edition_matches",
        "report_content_non_empty", "audit_chain_settled", "review_attempt_recorded",
    }
    art = svc.read_artifact(USER, task, artifact_id="reproduce.md")
    assert "[x] primary_report_bound" in art["content"]
    assert "0 LLM" in art["content"]
    assert out.ledger == []


async def test_reproduce_running_execution_recorded_as_integrity_gap(env, monkeypatch):
    svc, task, rid = await _reproduce_env(env)
    _boom_pipeline_seam(monkeypatch)
    _boom_adj_seam(monkeypatch)
    svc.record_execution(USER, task, tool="pipeline.execute.claim_stats",
                         args={}, execution_id=f"{rid}:audit:1")   # left RUNNING
    out = await _run(svc, task, rid)
    # degradable: the audit finds the fault, records it, still advances honestly
    assert out.kind == "advanced" and out.next_stage == "PUBLISH"
    assert _budget(svc, task)["calls"] == 0
    assert any(e["error_class"] == "handler_error"
               and "audit_chain_settled" in e["detail"] for e in out.ledger)
    rp = _proj(svc, task)["pipeline"]["reproduce"]
    failed = {c["name"] for c in rp["checks"] if not c["ok"]}
    assert failed == {"audit_chain_settled"}
    art = svc.read_artifact(USER, task, artifact_id="reproduce.md")
    assert "[ ] audit_chain_settled" in art["content"]


async def test_reproduce_missing_review_verdict_recorded(env, monkeypatch):
    svc, task, rid = await _reproduce_env(env, review_seed=False)
    _boom_pipeline_seam(monkeypatch)
    _boom_adj_seam(monkeypatch)
    out = await _run(svc, task, rid)
    assert out.kind == "advanced"
    assert any("review_attempt_recorded" in e["detail"] for e in out.ledger)


async def test_reproduce_ghost_edition_caught(env, monkeypatch):
    svc, task, rid = await _reproduce_env(env)
    _boom_pipeline_seam(monkeypatch)
    _boom_adj_seam(monkeypatch)
    svc.atomic_update_project(
        USER, task, lambda p: p.update(run_seq=p["run_seq"] + 1),
    )   # the report on disk is now from a previous edition
    out = await _run(svc, task, rid)
    # REPRODUCE records the ghost honestly but only PUBLISH hard-stops on it
    assert out.kind == "advanced"
    assert any("report_edition_matches" in e["detail"] for e in out.ledger)


# ═════════════════════════════ PUBLISH — absolute terminal hard gate ═════════

async def test_publish_promotes_substrate_terminal_advances(env, monkeypatch):
    assert PIPE_CONTRACTS["PUBLISH"].llm_calls == 0
    svc, task, rid = await _at(env, "PUBLISH")
    _boom_pipeline_seam(monkeypatch)
    _boom_adj_seam(monkeypatch)
    out = await _run(svc, task, rid)
    # PUBLISH is terminal: framework's _next_of -> None branch
    assert out.kind == "advanced" and out.next_stage is None
    assert "terminal stage reached" in out.turn_value
    assert _budget(svc, task)["calls"] == 0                   # HARD METRIC: 0 LLM
    pub = _proj(svc, task)["pipeline"]["publish"]
    assert pub["status"] == "PROMOTED" and pub["drive_asset_id"]
    assert pub["artifact"] == "report.md" and pub["version"] == 1
    assert "outputs" in (pub["drive_path"] or "") or "research/" in (pub["drive_path"] or "")
    assert out.ledger == []


async def test_publish_idempotent_reentry_reuses_promoted_view(env, monkeypatch):
    svc, task, rid = await _at(env, "PUBLISH")
    _boom_pipeline_seam(monkeypatch)
    _boom_adj_seam(monkeypatch)
    out1 = await _run(svc, task, rid)
    assert out1.kind == "advanced"
    first_id = _proj(svc, task)["pipeline"]["publish"]["drive_asset_id"]
    # a crash-rerun of the same node re-enters; the promoted view is idempotent
    out2 = await _run(svc, task, rid)
    assert out2.kind == "advanced" and out2.ledger == []
    pub = _proj(svc, task)["pipeline"]["publish"]
    assert pub["drive_asset_id"] == first_id and pub["idempotent"] is True


async def test_publish_without_report_blocks_zero_llm(env, monkeypatch):
    svc, task, rid = await _at(env, "PUBLISH", with_report=False)
    _boom_pipeline_seam(monkeypatch)
    _boom_adj_seam(monkeypatch)
    out = await _run(svc, task, rid)
    # constraint #3: a missing deliverable is terminal BLOCKED — never disguised
    assert out.kind == "blocked"
    assert out.structural["stage"] == "PUBLISH"
    assert out.structural["missing"] == "promoted_artifact"
    assert _proj(svc, task)["stage"] == "PUBLISH"             # stage untouched
    assert _budget(svc, task)["calls"] == 0
    assert "structural_stop" in _proj(svc, task)["pipeline"]


async def test_publish_ghost_report_refused_promotion_terminal(env, monkeypatch):
    svc, task, rid = await _at(env, "PUBLISH")
    _boom_pipeline_seam(monkeypatch)
    _boom_adj_seam(monkeypatch)
    svc.atomic_update_project(
        USER, task, lambda p: p.update(run_seq=p["run_seq"] + 1),
    )   # stale edition's report must NEVER publish as this run's output
    out = await _run(svc, task, rid)
    assert out.kind == "blocked"
    assert out.structural["missing"] == "promoted_artifact"
    assert "ghost" in out.structural["detail"]
    assert _proj(svc, task)["stage"] == "PUBLISH"
    assert _budget(svc, task)["calls"] == 0
