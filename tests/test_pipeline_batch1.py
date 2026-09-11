"""Batch-1 handler validation: DISCOVER / FRAME / EVIDENCE (sealed-spec hard metrics).

Every test pins the acceptance numbers directly:

* normal path LLM counts — DISCOVER=1, FRAME=1, EVIDENCE=1 (the EVIDENCE one is
  the adjudication closure through the SAME stage gate — the handler's own
  completion seam must never be touched);
* failure paths ride at most ONE repair (node total ≤ 2), never a third call;
* deterministic work (channels, dedup, fetch, chunking, graph writes) is 0 LLM.
"""
from __future__ import annotations

import asyncio
import json
import re
import uuid
from types import SimpleNamespace

import httpx
import pytest

import plugins.research.handlers as handlers
import plugins.research.pipeline as pipeline
import plugins.research.plugin as rplugin
from agent import Context, PluginManager, SkillRegistry, ToolRuntime
from core.infrastructure.request_context import set_request_user
from core.infrastructure.web_fetch import canonical_url as canonicalize
from plugins.research.plugin import ResearchService, register_research_plugins

USER = uuid.uuid4()


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


# ── network / LLM doubles ─────────────────────────────────────────────────────
_PUBLIC = "93.184.216.34"


def _article(fact: str, n: int = 12) -> str:
    paras = "".join(
        f"<p>{fact} sentence {i}: the cleaned article text must comfortably clear "
        "the usable floor so the page counts as a verifiable source.</p>"
        for i in range(n)
    )
    return f"<html><head><title>{fact}</title></head><body><nav>sidebar</nav>{paras}</body></html>"


def _install_fetch(monkeypatch, calls: list[str] | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(str(request.url))
        host = request.url.host
        if host.startswith("good") and host.endswith(".example"):
            return httpx.Response(200, text=_article(f"fact-{host}"))
        return httpx.Response(404, text="not found")

    monkeypatch.setattr(rplugin, "_FETCH_TRANSPORT_OVERRIDE", httpx.MockTransport(handler))
    monkeypatch.setattr(rplugin, "_FETCH_RESOLVER_OVERRIDE", lambda host: [_PUBLIC])


def _seam(monkeypatch, replies):
    """PIPELINE decision seam: records prompts, returns replies in order."""
    seen: list[str] = []

    async def fake(prompt: str, system: str) -> str:
        seen.append(prompt)
        return replies[min(len(seen) - 1, len(replies) - 1)]

    monkeypatch.setattr(pipeline, "PIPELINE_LLM_CALL", fake)
    return seen


def _seam_must_not_touch_llm(monkeypatch):
    async def boom(prompt: str, system: str) -> str:
        raise AssertionError("EVIDENCE handler made its own completion (double-call!)")

    monkeypatch.setattr(pipeline, "PIPELINE_LLM_CALL", boom)


# ── task scaffolding (fence identity aligned with the standalone node call) ───
async def _task(env, stage: str = "DISCOVER", title: str = "tomato research"):
    svc = ResearchService(drive=env.drive, scratch_root=env.scratch)
    task = (await svc.create_task(USER, title=title))["task_id"]
    rid = svc.begin_run(USER, task)["run_id"]
    base = svc.get_driver_checkpoint(USER, task)
    svc.atomic_update_project(
        USER, task,
        lambda p: p.__setitem__(
            "driver", {**base, "run_id": rid, "execution_id": f"{rid}:1:1"}
        ),
    )
    if stage != "DISCOVER":
        svc.atomic_update_project(USER, task, lambda p: p.__setitem__("stage", stage))
    return svc, task, rid


def _run(svc, task, rid, **kw):
    return pipeline.run_node(
        svc, USER, task, run_id=rid, execution_id=f"{rid}:1:1", turn_index=1, **kw,
    )


def _budget(svc, task):
    return svc.read_project(USER, task)["pipeline"]["last_node"]["budget"]


def _ok_keep(*urls):
    return json.dumps({"keep": [canonicalize(u) for u in urls], "notes": "solid hits"})


# ═════════════════════════════ DISCOVER ══════════════════════════════════════

async def test_discover_normal_path_exactly_one_llm(env, monkeypatch):
    svc, task, rid = await _task(env, title="health effects of tomatoes")
    fetched: list[str] = []
    _install_fetch(monkeypatch, fetched)

    async def web(q):
        return [
            {"url": "https://good1.example/a", "title": "G1", "text": "snippet one"},
            {"url": "https://good1.example/a", "title": "dup", "text": "dup"},  # deduped
            {"url": "https://good2.example/b", "title": "G2", "text": "snippet two"},
        ]

    async def social(q):
        return [{"url": "https://good3.example/c", "title": "S3", "text": "thread"}]

    async def rag(q):
        return [{"url": "https://good4.example/d", "title": "R4", "text": "passage"}]

    seen = _seam(monkeypatch, [_ok_keep(
        "https://good1.example/a", "https://good2.example/b",
    )])
    out = await _run(svc, task, rid, extras={
        "channel_web": web, "channel_social": social, "channel_rag": rag,
    })
    assert out.kind == "advanced" and out.next_stage == "FRAME"
    assert svc.read_project(USER, task)["stage"] == "FRAME"
    # HARD METRIC: exactly ONE semantic call — the triage. Channels/fetch = 0 LLM.
    assert len(seen) == 1
    assert _budget(svc, task)["calls"] == 1
    assert out.ledger == []                       # rag wired → no degrade lines
    # dedup really happened: good1 fetched once despite two channel rows
    assert sum(1 for u in fetched if "good1" in u) == 1
    assert all(f"good{i}" in " ".join(fetched) for i in (1, 2, 3, 4))
    # honest corpus artifact + machine-readable URL set for downstream nodes
    art = svc.read_artifact(USER, task, artifact_id="corpus.md")
    assert "Research corpus" in art["content"] and "good1" in art["content"]
    assert "good3" not in art["content"]          # triage kept-subset, not everything
    corpus = svc.read_project(USER, task)["pipeline"]["corpus"]
    assert [canonicalize(u) for u in
            ("https://good1.example/a", "https://good2.example/b")] == corpus["urls"]
    assert corpus["channels_ran"] == {"web": 3, "social": 1, "rag": 1, "materials": 0}


async def test_discover_channel_micro_timeout_degrades_not_kills(env, monkeypatch):
    svc, task, rid = await _task(env, title="health effects of tomatoes")
    _install_fetch(monkeypatch)
    monkeypatch.setattr(handlers, "CHANNEL_TIMEOUT_S", 0.05)

    async def slow_web(q):
        await asyncio.sleep(0.5)
        return []                                  # never reached

    async def fast(q):
        return [{"url": "https://good1.example/a", "title": "G1", "text": "x"}]

    seen = _seam(monkeypatch, [_ok_keep("https://good1.example/a")])
    out = await _run(svc, task, rid, extras={
        "channel_web": slow_web, "channel_social": fast, "channel_rag": fast,
    })
    # constraint #2: the slow channel is a LEDGER line, not a dead node
    assert out.kind == "advanced"
    kinds = [(e["error_class"], e["missing"]) for e in out.ledger]
    assert ("source_unavailable", "web") in kinds
    assert len(seen) == 1 and _budget(svc, task)["calls"] == 1  # triage survived


async def test_discover_repair_once_then_mechanical_fallback(env, monkeypatch):
    svc, task, rid = await _task(env, title="health effects of tomatoes")
    _install_fetch(monkeypatch)

    async def web(q):
        return [{"url": "https://good1.example/a", "title": "G1", "text": "x"}]

    async def other(q):
        return []

    bad = json.dumps({"keep": ["https://bogus.example/z"]})   # unknown URL
    seen = _seam(monkeypatch, [bad, bad])
    out = await _run(svc, task, rid, extras={
        "channel_web": web, "channel_social": other, "channel_rag": other,
    })
    assert out.kind == "advanced"
    # HARD METRIC: two attempts max — repair once, NEVER a third call
    assert len(seen) == 2 and "VIOLATIONS" in seen[1]
    assert _budget(svc, task)["calls"] == 2
    assert [e["error_class"] for e in out.ledger] == ["degraded_decision"]
    # honest advance: mechanical corpus over every usable source anyway
    art = svc.read_artifact(USER, task, artifact_id="corpus.md")
    assert "good1" in art["content"]
    corpus = svc.read_project(USER, task)["pipeline"]["corpus"]
    assert corpus["urls"] == [canonicalize("https://good1.example/a")]


# ═════════════════════════════ FRAME ═════════════════════════════════════════

async def _frame_setup(env, monkeypatch):
    svc, task, rid = await _task(env, stage="FRAME", title="tomato framing")
    cu = canonicalize("https://good1.example/a")
    await svc.write_scratch(
        USER, task, artifact_id="corpus.md",
        content="# Research corpus: tomatoes\n\n## G1\nSource: " + cu
                + "\n\nCooking tomatoes releases lycopene, a carotenoid antioxidant.",
    )

    def _seed(p):
        p.setdefault("pipeline", {})["corpus"] = {"query": "tomatoes", "urls": [cu]}
    svc.atomic_update_project(USER, task, _seed)
    return svc, task, rid, cu


async def test_frame_normal_path_exactly_one_llm(env, monkeypatch):
    svc, task, rid, cu = await _frame_setup(env, monkeypatch)
    reply = json.dumps({
        "question": "Does home cooking measurably increase lycopene bioavailability in tomatoes?",
        "in_scope": "cooking methods, bioassays",
        "out_of_scope": "ketchup marketing",
        "claims": [
            {"id": "k1", "statement": "Cooking increases lycopene bioavailability",
             "strength": "medium", "citations": [cu, "https://bogus.example/x"]},
            {"id": "k2", "statement": "Raw tomato lycopene is poorly absorbed",
             "strength": "banana", "citations": [cu]},         # bad strength → retried bare
            {"statement": "Sauce beats paste"},                  # id-less row → k3
            "junk row",                                           # dropped
        ],
    })
    seen = _seam(monkeypatch, [reply])
    out = await _run(svc, task, rid)
    assert out.kind == "advanced" and out.next_stage == "EVIDENCE"
    assert len(seen) == 1 and _budget(svc, task)["calls"] == 1   # HARD METRIC: 1
    assert out.ledger == []
    proj = svc.read_project(USER, task)
    assert proj["stage"] == "EVIDENCE"
    assert proj["research_question"].startswith("Does home cooking")
    graph = svc._load_graph(USER, task)
    claims = {n["id"]: n for n in graph["nodes"] if n.get("type") == "Claim"}
    assert set(claims) == {"k1", "k2", "k3"}
    assert claims["k1"]["citations"] == [cu]      # hallucinated citation dropped
    assert claims["k2"].get("strength") is None   # invalid strength fell back bare
    assert any(n.get("type") == "Question" for n in graph["nodes"])
    # deterministic writes cost nothing extra
    art = svc.read_artifact(USER, task, artifact_id="research_question.md")
    assert "lycopene bioavailability" in art["content"]
    assert proj["pipeline"]["frame"]["claims_dropped"] == [
        "claim row 3: not an object with a statement"]


async def test_frame_missing_question_is_structural_terminal(env, monkeypatch):
    svc, task, rid, cu = await _frame_setup(env, monkeypatch)
    bad = json.dumps({"question": "tomatoes?", "claims": []})   # too short / not falsifiable
    seen = _seam(monkeypatch, [bad, bad])
    out = await _run(svc, task, rid)
    # repair once (≤2 calls), then the dead node TERMINALIZES — no force advance
    assert len(seen) == 2 and _budget(svc, task)["calls"] == 2
    assert out.kind == "blocked"
    assert out.structural["missing"] == "research_question"
    assert svc.read_project(USER, task)["stage"] == "FRAME"      # state untouched
    flag = svc.read_project(USER, task)["pipeline"]["structural_stop"]
    assert flag["stage"] == "FRAME" and flag["missing"] == "research_question"


async def test_frame_missing_corpus_is_structural(env, monkeypatch):
    svc, task, rid = await _task(env, stage="FRAME")            # no corpus seeded
    _seam(monkeypatch, ["{}"])
    out = await _run(svc, task, rid)
    assert out.kind == "blocked"
    assert out.structural["missing"] == "corpus"
    assert _budget(svc, task)["calls"] == 0     # refusal was deterministic — 0 LLM


# ═════════════════════════════ EVIDENCE ══════════════════════════════════════

async def _evidence_task(env, monkeypatch, llm_reply, *, claims=(("c1", "tomatoes are a fruit"),
                                                                 ("c2", "ketchup is a smoothie"))):
    svc, task, rid = await _task(env, stage="EVIDENCE")
    for cid, stmt in claims:
        svc.record_node(USER, task, node={
            "id": cid, "type": "Claim", "label": stmt, "statement": stmt,
            "strength": "medium", "citations": [canonicalize("https://good1.example/a")],
        })
    cu1, cu2 = canonicalize("https://good1.example/a"), canonicalize("https://good2.example/b")

    def _seed(p):
        p.setdefault("pipeline", {})["corpus"] = {
            "query": "tom", "urls": [cu1, cu2],
        }
    svc.atomic_update_project(USER, task, _seed)
    _install_fetch(monkeypatch)
    prompts: list[str] = []

    async def adj(prompt: str, system_prompt: str) -> str:
        prompts.append(prompt)
        return llm_reply(prompt) if callable(llm_reply) else llm_reply

    monkeypatch.setattr(rplugin, "_ADJ_LLM_CALL", adj)
    _seam_must_not_touch_llm(monkeypatch)        # zero own completions, provably
    return svc, task, rid, prompts


def _sids_in(prompt: str) -> list[str]:
    return re.findall(r'"source_id": "(S\d+)"', prompt)


async def test_evidence_routes_the_single_llm_through_the_shared_gate(env, monkeypatch):
    def reply(prompt):
        cids = sorted(set(re.findall(r'"claim_id": "(c\d+)"', prompt)))
        return json.dumps({"results": [
            {"source_id": s, "claim_id": c, "verdict": "supports"}
            for s in _sids_in(prompt) for c in cids
        ]})

    svc, task, rid, prompts = await _evidence_task(env, monkeypatch, reply)
    out = await _run(svc, task, rid)
    assert out.kind == "advanced" and out.next_stage == "DESIGN"
    # HARD METRIC: EVIDENCE=1 — the closure IS the node's only LLM throughput
    assert len(prompts) == 1
    assert _budget(svc, task)["calls"] == 1
    # …and it rode the STAGE GATE: the run-level budget metered it
    snap = _budget(svc, task)
    assert snap["tokens_in_est"] > 0
    graph = svc._load_graph(USER, task)
    kinds = {e["src"] for e in graph["edges"] if e.get("kind") == "supports"}
    assert kinds == {"c1", "c2"}                 # verdicts committed
    assert out.ledger == []
    assert not (svc.read_project(USER, task)["pipeline"].get("known_gaps"))


async def test_evidence_semantic_gaps_land_in_known_gaps_honestly(env, monkeypatch):
    def reply(prompt):
        cids = sorted(set(re.findall(r'"claim_id": "(c\d+)"', prompt)))
        return json.dumps({"results": [
            {"source_id": s, "claim_id": c, "verdict": "insufficient"}
            for s in _sids_in(prompt) for c in cids
        ]})

    svc, task, rid, prompts = await _evidence_task(env, monkeypatch, reply)
    out = await _run(svc, task, rid)
    assert out.kind == "advanced"                # gaps surface, chain never parks
    assert len(prompts) == 1 and _budget(svc, task)["calls"] == 1
    gaps = svc.read_project(USER, task)["pipeline"]["known_gaps"]
    assert {g["claim_id"] for g in gaps} == {"c1", "c2"}


async def test_evidence_zero_pending_claims_is_pure_python(env, monkeypatch):
    svc, task, rid = await _task(env, stage="EVIDENCE")         # graph has no claims
    _install_fetch(monkeypatch)
    _seam_must_not_touch_llm(monkeypatch)
    called: list[str] = []

    async def adj(prompt, system_prompt):
        called.append("x")
        return "{}"

    monkeypatch.setattr(rplugin, "_ADJ_LLM_CALL", adj)
    out = await _run(svc, task, rid)
    assert out.kind == "advanced" and out.next_stage == "DESIGN"
    assert _budget(svc, task)["calls"] == 0      # deterministic 0-LLM pass-through
    assert called == []
