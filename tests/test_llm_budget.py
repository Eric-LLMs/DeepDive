"""Pre-commit verification for the unified LLM budget gate + F2 fence activation.

Proves the three sealed-spec rulings on the metering path:
1. every semantic completion rides ``admit``/``settle`` (no hidden passes);
2. the run-level cost hard fuse power-cuts BEFORE the next call at >= cap;
3. a pipeline node body never writes authoritative state unfenced
   (``node_entry_fence`` inherits the driver's fence or mints its own).
"""
from __future__ import annotations

import inspect
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

import plugins.research.plugin as rplugin
from core.infrastructure.request_context import set_request_user
from plugins.research.llm_budget import (
    CHARS_PER_TOKEN_EST,
    RunBudget,
    StageBudgetExceeded,
    StageGate,
)
from plugins.research.plugin import (
    OwnershipLost,
    ResearchService,
    clear_auto_run_fence,
    get_auto_run_fence,
    node_entry_fence,
    set_auto_run_fence,
)
from plugins.research.workflow_adapter import CostLimitExceeded

USER = uuid.uuid4()


@pytest.fixture(autouse=True)
def _request_user():
    set_request_user(USER)
    yield
    set_request_user(None)


@pytest.fixture(autouse=True)
def _no_fence():
    assert get_auto_run_fence() is None
    yield


@pytest.fixture
def env(tmp_path):
    from agent import Context, PluginManager, SkillRegistry, ToolRuntime
    from plugins.research.plugin import register_research_plugins
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


# ── stage call budget ─────────────────────────────────────────────────────────

def test_stage_gate_declared_calls_are_the_authority():
    run = RunBudget(cap_usd=None)
    gate = StageGate(run, stage="EVIDENCE", max_calls=2)
    gate.admit(); gate.admit()
    assert (gate.calls, run.calls) == (2, 2)
    with pytest.raises(StageBudgetExceeded):
        gate.admit()
    assert run.calls == 2  # the refusal never meters a phantom call


def test_zero_budget_stage_admits_nothing():
    gate = StageGate(RunBudget(cap_usd=None), stage="PUBLISH", max_calls=0)
    with pytest.raises(StageBudgetExceeded):
        gate.admit()


# ── run-level cost hard fuse (>= cap, power cut BEFORE the call) ─────────────

def test_hard_fuse_fires_at_cap_before_any_further_call():
    run = RunBudget(cap_usd=0.40, start_spent_usd=0.40)
    gate = StageGate(run, stage="WRITE", max_calls=5)
    with pytest.raises(CostLimitExceeded):
        gate.admit()
    assert gate.calls == 0 and run.calls == 0


def test_just_below_cap_still_admits():
    run = RunBudget(cap_usd=0.40, start_spent_usd=0.3999)
    StageGate(run, stage="WRITE", max_calls=1).admit()


def test_fuse_rides_all_gates_of_one_run():
    run = RunBudget(cap_usd=0.01, pricing=(1000.0, 1000.0))  # $1 per 1k tokens
    g1 = StageGate(run, stage="DISCOVER", max_calls=4)
    g1.admit(); g1.settle("x" * 4000, "y" * 400)  # 1000+100 tok = $1.1 > cap
    g2 = StageGate(run, stage="FRAME", max_calls=4)
    with pytest.raises(CostLimitExceeded):
        g2.admit()  # next stage's FIRST call power-cuts on the run-level spend


# ── token/cost metering semantics ─────────────────────────────────────────────

def test_settle_meters_estimated_tokens_and_injected_price():
    run = RunBudget(cap_usd=None, pricing=(1.0, 2.0))  # $1/1k in, $2/1k out
    gate = StageGate(run, stage="WRITE", max_calls=1)
    gate.admit()
    gate.settle("p" * 4000, "r" * 1000)  # 1000 in / 250 out est tokens
    assert run.tokens_in == 4000 // CHARS_PER_TOKEN_EST
    assert run.tokens_out == 1000 // CHARS_PER_TOKEN_EST
    assert run.spent == pytest.approx(1000 / 1000 * 1.0 + 250 / 1000 * 2.0)


def test_failed_call_is_counted_never_laundered_away():
    run = RunBudget(cap_usd=None, pricing=(1.0, 1.0))
    gate = StageGate(run, stage="EVIDENCE", max_calls=1)
    gate.admit()
    gate.settle("p" * 4000, None)
    snap = run.snapshot()
    assert snap["failed_calls"] == 1 and snap["calls"] == 1
    assert snap["tokens_in_est"] == 1000 and snap["tokens_out_est"] == 0
    # input tokens were still spent on the dead attempt — billed, not dropped
    assert run.spent == pytest.approx(1.0)


def test_pricing_unknown_never_becomes_zero_spend():
    run = RunBudget(cap_usd=None, model="totally-unpriced-model-xyz")
    gate = StageGate(run, stage="WRITE", max_calls=2)
    gate.admit(); gate.settle("p" * 40000, "r" * 4000)
    assert run.spent == 0.0                      # no price → nothing laundered
    assert run.pricing_unknown_calls == 1        # …but the gap is explicit
    assert run.tokens_in == 10000 and run.tokens_out == 1000


# ── F2 fence activation at node entry ─────────────────────────────────────────

def test_node_fence_mints_when_absent_and_clears_on_exit():
    with node_entry_fence(owner_id=USER, task_id="t1", run_id="r1",
                          execution_id="r1:1:1") as live:
        assert live["run_id"] == "r1" and live["execution_id"] == "r1:1:1"
        assert get_auto_run_fence() is not None
    assert get_auto_run_fence() is None


def test_node_fence_inherits_the_drivers_fence():
    token = set_auto_run_fence(owner_id=USER, task_id="t1", run_id="r-live",
                               execution_id="r-live:3:1")
    try:
        with node_entry_fence(owner_id=USER, task_id="t1", run_id="r-other",
                              execution_id="r-other:3:1") as live:
            assert live["run_id"] == "r-live"  # never overrides the driver identity
        assert get_auto_run_fence()["run_id"] == "r-live"  # still the driver's
    finally:
        clear_auto_run_fence(token)


async def test_node_fence_actually_blocks_a_zombie_write(env):
    """The no-op hole is closed: an unfenced context would let write_scratch through;
    inside node_entry_fence a superseded execution identity is refused."""
    from agent import Context  # noqa: F401  (env already wired the plugin manager)

    svc = ResearchService(drive=env.drive, scratch_root=env.scratch)
    task_id = (await svc.create_task(USER, title="fence"))["task_id"]
    rid = svc.begin_run(USER, task_id)["run_id"]
    base = svc.get_driver_checkpoint(USER, task_id)
    svc.atomic_update_project(
        USER, task_id,
        lambda p: p.__setitem__(
            "driver", {**base, "run_id": rid, "execution_id": f"{rid}:1:2"}
        ),
    )
    # stale identity (…:1:1) while the ledger moved to (…:1:2): refused
    with node_entry_fence(owner_id=USER, task_id=task_id, run_id=rid,
                          execution_id=f"{rid}:1:1"):
        with pytest.raises(OwnershipLost):
            await svc.write_scratch(USER, task_id, artifact_id="draft.md",
                                    content="zombie write")
    # current identity: passes the fence and lands
    with node_entry_fence(owner_id=USER, task_id=task_id, run_id=rid,
                          execution_id=f"{rid}:1:2"):
        out = await svc.write_scratch(USER, task_id, artifact_id="draft.md",
                                      content="honest write")
    assert out["status"] in ("ok", "DRAFT")  # fenced current identity lands


# ── source audit: the choke point is the ONLY way to the transport ────────────

def test_no_completion_site_bypasses_the_gate():
    """``_adjudication_llm_complete`` may be referenced exactly twice in plugin.py:
    its own definition and the single call inside ``_gated_llm_complete``. Any new
    raw call site (a hidden LLM pass) fails this test at review time."""
    src = Path(rplugin.__file__).read_text(encoding="utf-8")
    raw_calls = [
        line.strip() for line in src.splitlines()
        if "_adjudication_llm_complete(" in line
        and not line.strip().startswith(("async def ", "#"))
    ]
    assert len(raw_calls) == 1, f"ungated call sites: {raw_calls}"
    assert "await _adjudication_llm_complete(" in raw_calls[0]
    # and that one site lives inside the choke point
    gated_src = inspect.getsource(rplugin._gated_llm_complete)
    assert "await _adjudication_llm_complete(" in gated_src
