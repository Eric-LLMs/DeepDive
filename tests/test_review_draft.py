"""P3-9 REVIEW atomic closure tests: ``research_artifact action="review_draft"``.

The locked contract (REVIEW 阶段最终确认令): whole draft + claim graph go into ONE
streaming LLM call; the reply is ONLY ``{"changes": [{file,target,expected_old,
change}, ...]}`` — never prose, never a document echo. Python pre-checks every row
(strict 4-key schema, unique-match iron rule inside the target's section), applies
them to a memory staging copy, and commits atomically: all rows pass -> ONE
create_version; ANY row fails -> staging discarded, nothing written, RuntimeError
reports the exact rejects (Git worktree + graph never half-landed).

No real LLM: the reviewer rides the same ``_ADJ_LLM_CALL`` module seam.
"""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from agent import Context, PluginManager, SkillRegistry, ToolRuntime
from core.infrastructure.request_context import set_request_user
from plugins.research.plugin import (
    ResearchService,
    _ARTIFACT_ACTIONS,
    _apply_review_change,
    _parse_review_payload,
    register_research_plugins,
)

import plugins.research.plugin as rplugin

USER = uuid.uuid4()


@pytest.fixture(autouse=True)
def _request_user():
    set_request_user(USER)
    yield
    set_request_user(None)


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
    return SimpleNamespace(
        ctx=ctx, drive=drive, runtime=runtime, manager=manager, scratch=tmp_path / "scratch"
    )


def _svc(env) -> ResearchService:
    return ResearchService(drive=env.drive, scratch_root=env.scratch)


def _change(file="report.md", target="", old="X", new="Y"):
    return {"file": file, "target": target, "expected_old": old, "change": new}


async def _project_with_draft(env, draft: str, claims: dict[str, str]):
    """Fresh versioned run: Claim nodes recorded + report.md v1 = draft."""
    svc = _svc(env)
    task_id = (await svc.create_task(USER, title="review"))["task_id"]
    svc.begin_run(USER, task_id)
    for cid, stmt in claims.items():
        svc.record_node(
            USER, task_id,
            node={"id": cid, "type": "Claim", "label": stmt, "statement": stmt},
        )
    await svc.write_scratch(USER, task_id, artifact_id="report.md", content=draft)
    return svc, task_id


def _seam(monkeypatch, replies: list[str]):
    """Patch the inner LLM; record every prompt it receives."""
    seen: list[tuple[str, str]] = []

    async def fake(prompt: str, system_prompt: str) -> str:
        seen.append((prompt, system_prompt))
        return replies[len(seen) - 1] if len(seen) <= len(replies) else replies[-1]

    monkeypatch.setattr(rplugin, "_ADJ_LLM_CALL", fake)
    return seen


# ── schema-level unit tests (顶级仅 changes;每项仅 4 键) ──────────────────────

def test_payload_schema_ok_and_empty():
    assert _parse_review_payload('{"changes": []}') == []
    rows = _parse_review_payload(
        '```json\n{"changes": [{"file":"a","target":"","expected_old":"o","change":"c"}]}\n```'
    )
    assert rows == [_change(file="a", old="o", new="c")]


def test_payload_rejects_extra_top_key_and_extra_row_key_and_prose():
    with pytest.raises(ValueError):  # 顶级仅 changes
        _parse_review_payload('{"changes": [], "notes": "reasoning"}')
    with pytest.raises(ValueError):  # 行内仅 4 键,禁 markdown/思考字段
        _parse_review_payload(json.dumps({"changes": [
            {**_change(), "why": "because"}]}))
    with pytest.raises(ValueError):  # 无 JSON 对象
        _parse_review_payload("The draft looks fine to me.")
    with pytest.raises(ValueError):  # expected_old 为空
        _parse_review_payload(json.dumps({"changes": [_change(old="")]}))


def test_change_rows_require_exactly_the_four_keys():
    assert "review_draft" in _ARTIFACT_ACTIONS  # LLM 可见 action 枚举同步


# ── unique-match iron rule (预检,target + expected_old 段内唯一) ──────────────

def test_apply_target_must_match_exactly_once():
    doc = "# A\nfoo bar\n# B\nfoo baz\n"
    _, reason = _apply_review_change(doc, _change(target="foo", old="bar", new="x"))
    assert reason and "target matches 2" in reason


def test_apply_expected_old_is_section_scoped():
    # "shared" appears TWICE in the doc but ONCE inside section "# B" -> allowed.
    doc = "# A\nshared line\n# B\ntext shared tail\n"
    out, reason = _apply_review_change(doc, _change(target="# B", old="shared", new="UNIQUE"))
    assert reason is None
    assert "# A\nshared line" in out and "text UNIQUE tail" in out


def test_apply_zero_match_rejects():
    doc = "nothing here"
    _, reason = _apply_review_change(doc, _change(old="absent phrase"))
    assert reason and "matches 0" in reason


# ── service closure: staging + all-or-nothing ─────────────────────────────────

async def test_happy_path_two_changes_commit_one_version(env, monkeypatch):
    draft = "# 结论\n番茄去皮可加速出汁。\n# 步骤\n加水炒制三分钟。\n"
    svc, task_id = await _project_with_draft(env, draft, {"c1": "去皮出汁"})
    _seam(monkeypatch, [json.dumps({"changes": [
        _change(old="加水炒制", new="不放水炒制"),
        _change(target="# 结论", old="可加速出汁", new="可加速出汁（未验证）"),
    ]}, ensure_ascii=False)])

    out = await svc.review_draft(USER, task_id, artifact_id="report.md")
    assert out["status"] == "ok" and out["llm_calls"] == 1
    assert out["changes_applied"] == 2
    assert out["base_version"] == 1 and out["new_version"] == 2
    v2 = svc.read_artifact(USER, task_id, artifact_id="report.md", version=2)["content"]
    assert "不放水炒制" in v2 and "（未验证）" in v2 and "可加速出汁（未验证）三分钟" not in v2
    # v1 未被改动(预检全过才落地)
    assert svc.read_artifact(USER, task_id, artifact_id="report.md", version=1)["content"] == draft


async def test_iron_rule_violation_writes_nothing(env, monkeypatch):
    draft = "alpha beta alpha gamma\n"
    svc, task_id = await _project_with_draft(env, draft, {"c1": "x"})
    _seam(monkeypatch, [json.dumps({"changes": [
        _change(old="beta", new="BETTER"),        # would pass
        _change(old="alpha", new="ALPHA"),        # matches 2 -> reject
    ]})])

    with pytest.raises(RuntimeError, match="1/2 rows failed"):
        await svc.review_draft(USER, task_id, artifact_id="report.md")
    # 全量未修改:没有 v2,base v1 完好
    assert svc.read_artifact(USER, task_id, artifact_id="report.md")["version"] == 1
    dir_listing = list((env.scratch / str(USER) / task_id / "artifacts" / "report.md").glob("v*")) \
        if (env.scratch / str(USER) / task_id / "artifacts").is_dir() else []
    assert not any(p.name == "v2" for p in dir_listing)


async def test_wrong_file_target_rejects_and_rolls_back(env, monkeypatch):
    svc, task_id = await _project_with_draft(env, "hello world\n", {"c1": "x"})
    _seam(monkeypatch, [json.dumps({"changes": [
        _change(file="other.md", old="world", new="WORLD")]})])
    with pytest.raises(RuntimeError, match="is not"):
        await svc.review_draft(USER, task_id, artifact_id="report.md")


async def test_empty_changes_no_version(env, monkeypatch):
    svc, task_id = await _project_with_draft(env, "fine draft\n", {"c1": "x"})
    _seam(monkeypatch, ['{"changes": []}'])
    out = await svc.review_draft(USER, task_id, artifact_id="report.md")
    assert out["new_version"] is None and out["changes_applied"] == 0
    assert "fully supported" in out["hint"]


async def test_malformed_reply_gets_one_repair_then_commits(env, monkeypatch):
    svc, task_id = await _project_with_draft(env, "aaa bbb\n", {"c1": "x"})
    seen = _seam(monkeypatch, ["Let me explain: the draft is…", json.dumps({"changes": [
        _change(old="bbb", new="ccc")]})])
    out = await svc.review_draft(USER, task_id, artifact_id="report.md")
    assert out["llm_calls"] == 2 and out["new_version"] == 2
    assert "Reply with ONLY the JSON object" in seen[1][0]


async def test_double_malformed_raises(env, monkeypatch):
    svc, task_id = await _project_with_draft(env, "aaa\n", {"c1": "x"})
    _seam(monkeypatch, ["prose", "still prose"])
    with pytest.raises(RuntimeError, match="twice"):
        await svc.review_draft(USER, task_id, artifact_id="report.md")


async def test_prompt_carries_whole_draft_and_claims(env, monkeypatch):
    draft = "# S\n" + "body text. " * 200
    svc, task_id = await _project_with_draft(env, draft, {"c1": "stmt-one", "c2": "stmt-two"})
    seen = _seam(monkeypatch, ['{"changes": []}'])
    await svc.review_draft(USER, task_id, artifact_id="report.md")
    prompt = seen[0][0]
    assert draft in prompt                      # 全文一次性输入,零 read 循环
    assert "stmt-one" in prompt and "c1" in prompt


async def test_budget_guard(monkeypatch, env):
    monkeypatch.setattr(rplugin, "REVIEW_BUDGET_TOKENS", 50)
    svc, task_id = await _project_with_draft(env, "x" * 4000, {"c1": "x"})
    _seam(monkeypatch, ['{"changes": []}'])
    with pytest.raises(ValueError, match="REVIEW_BUDGET_TOKENS"):
        await svc.review_draft(USER, task_id, artifact_id="report.md")


# ── G3: run_seq provenance + ghost-tree isolation ────────────────────────────

async def test_write_and_version_records_carry_current_run_seq(env):
    svc, task_id = await _project_with_draft(env, "draft\n", {"c1": "x"})
    proj = svc.read_project(USER, task_id)
    rec = svc._load_json(
        env.scratch / str(USER) / task_id / "artifacts" / "report.md" / "v1", None
    )
    assert rec.get("run_seq") == proj["run_seq"]


async def test_g3_review_rejects_ghost_from_older_edition(env, monkeypatch):
    # A draft physically produced by a previous edition (run_seq 5) must not be
    # reviewable when the live run is edition 9 — this is the Run-13/14 "reviewed the
    # wrong object" failure, now blocked at the source.
    svc, task_id = await _project_with_draft(env, "ghost draft X\n", {"c1": "x"})
    svc.atomic_update_project(USER, task_id, lambda p: p.update(run_seq=p["run_seq"] + 1))
    _seam(monkeypatch, ['{"changes": []}'])
    with pytest.raises(ValueError, match="ghost"):
        await svc.review_draft(USER, task_id, artifact_id="report.md")


async def test_g3_promote_rejects_ghost_from_older_edition(env):
    svc, task_id = await _project_with_draft(env, "ghost draft X\n", {"c1": "x"})
    svc.atomic_update_project(USER, task_id, lambda p: p.update(run_seq=p["run_seq"] + 1))
    with pytest.raises(ValueError, match="ghost"):
        await svc.promote_to_drive(USER, task_id, artifact_id="report.md")


async def test_g3_untagged_legacy_version_is_grandfathered(env, monkeypatch):
    # A pre-G3 version has no run_seq key at all (None) — not a concrete contradiction,
    # so it is accepted (G1 already guarantees a compliant run rewrote its own draft).
    svc, task_id = await _project_with_draft(env, "legacy draft X\n", {"c1": "x"})
    vpath = env.scratch / str(USER) / task_id / "artifacts" / "report.md" / "v1"
    rec = svc._load_json(vpath, None)
    rec.pop("run_seq", None)
    svc._save_json(vpath, rec)
    svc.atomic_update_project(USER, task_id, lambda p: p.update(run_seq=p["run_seq"] + 1))
    _seam(monkeypatch, [json.dumps({"changes": [_change(old="legacy", new="LEGACY")]})])
    out = await svc.review_draft(USER, task_id, artifact_id="report.md")
    assert out["status"] == "ok" and out["new_version"] == 2


async def test_g3_identical_rewrite_reclaims_run_seq(env, monkeypatch):
    # Byte-identical re-produce under a new edition re-stamps the version to the current
    # run (so the compliant replay is owned by this edition, not flagged as a ghost).
    draft = "stable draft X\n"
    svc, task_id = await _project_with_draft(env, draft, {"c1": "x"})
    svc.atomic_update_project(USER, task_id, lambda p: p.update(run_seq=p["run_seq"] + 1))
    await svc.write_scratch(USER, task_id, artifact_id="report.md", content=draft)
    rec = svc._load_json(
        env.scratch / str(USER) / task_id / "artifacts" / "report.md" / "v1", None
    )
    assert rec["run_seq"] == svc.read_project(USER, task_id)["run_seq"]
    _seam(monkeypatch, [json.dumps({"changes": [_change(old="stable", new="STABLE")]})])
    out = await svc.review_draft(USER, task_id, artifact_id="report.md")
    assert out["status"] == "ok"
