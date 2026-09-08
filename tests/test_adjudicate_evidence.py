"""P3-8 EVIDENCE atomic closure tests: ``research_evidence action="adjudicate"``.

The locked contract (终极实施令): the outer agent triggers EXACTLY ONE EVIDENCE
action; everything else — fetch (P3-5 cap-5 batches internally), deterministic
Representative-Chunk extraction (pure Python, NO LLM summarisation), token-budget
sizing, batched single-pass relevance+adjudication LLM calls (split only on budget
overflow), ID validation/reconciliation, and the ONE ``verify_batch`` atomic commit
— runs inside ``plugins/research``. The agent has ZERO awareness of any split.

No real network, no real LLM: fetch rides the P3-5 ``MockTransport`` seam, and the
adjudicator rides the ``_ADJ_LLM_CALL`` module seam.
"""
from __future__ import annotations

import json
import re
import uuid
from types import SimpleNamespace

import httpx
import pytest

from agent import Context, PluginManager, SkillRegistry, ToolRuntime
from agent.engine.decisions import ToolExecution
from core.infrastructure.request_context import set_request_user
from plugins.research.monitor import MUTATING_ACTIONS
from plugins.research.plugin import (
    ADJUDICATION_SNIPPET_CHARS,
    ResearchService,
    _EVIDENCE_ACTIONS,
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


def _graph(env, task_id: str) -> dict:
    return ResearchService._load_json(
        env.scratch / str(USER) / task_id / "graph.json", {"nodes": [], "edges": []}
    )


def _proj(env, task_id: str) -> dict:
    return ResearchService._load_json(
        env.scratch / str(USER) / task_id / "project.json", None
    )


async def _new_run(env) -> tuple[ResearchService, str]:
    svc = _svc(env)
    task_id = (await svc.create_task(USER, title="adjudicate"))["task_id"]
    svc.begin_run(USER, task_id)
    return svc, task_id


def _record_claim(svc: ResearchService, task_id: str, cid: str, statement: str) -> None:
    svc.record_node(
        USER, task_id,
        node={"id": cid, "type": "Claim", "label": statement, "statement": statement},
    )


# ── network double (one distinct fact phrase per host) ───────────────────────
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
    if host.startswith("good") and host.endswith(".example"):
        return httpx.Response(200, text=_article(f"fact-{host}"))
    return httpx.Response(404, text="not found")


def _install_fetch(monkeypatch, *, calls: list[str] | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(str(request.url))
        return _page_handler(request)

    monkeypatch.setattr(rplugin, "_FETCH_TRANSPORT_OVERRIDE", httpx.MockTransport(handler))
    monkeypatch.setattr(rplugin, "_FETCH_RESOLVER_OVERRIDE", lambda host: [_PUBLIC])


class _LlmStub:
    """Records prompts, returns queued replies (the LAST one repeats forever)."""

    def __init__(self, *replies):
        self.prompts: list[str] = []
        self._replies = list(replies)

    async def __call__(self, prompt: str, system_prompt: str) -> str:
        self.prompts.append(prompt)
        return self._replies.pop(0) if len(self._replies) > 1 else self._replies[0]


def _install_llm(monkeypatch, *replies) -> _LlmStub:
    stub = _LlmStub(*replies)
    monkeypatch.setattr(rplugin, "_ADJ_LLM_CALL", stub)
    return stub


def _supports(claim: str, *sids: str) -> str:
    return json.dumps(
        {"results": [{"source_id": s, "claim_id": claim, "verdict": "supports"} for s in sids]}
    )


def _sids_in(prompt: str) -> list[str]:
    return re.findall(r'"source_id": "(S\d+)"', prompt)


def _ticket_edges(graph: dict, claim_id: str) -> list[str]:
    return [e["kind"] for e in graph["edges"] if e.get("src") == claim_id and e.get("kind") in ("supports", "contradicts")]


# ══════════════════════════════════════════════════════════════════════════════
# Contract surfaces
# ══════════════════════════════════════════════════════════════════════════════

class TestAdjudicateContractSurfaces:
    def test_action_is_visible_and_monitor_wired(self):
        assert "adjudicate" in _EVIDENCE_ACTIONS
        assert "adjudicate" in MUTATING_ACTIONS["research_evidence"]

    async def test_dispatch_through_the_runtime_router(self, env, monkeypatch):
        svc, task_id = await _new_run(env)
        _record_claim(svc, task_id, "c1", "tomatoes are a fruit")
        _install_fetch(monkeypatch)
        _install_llm(monkeypatch, _supports("c1", "S1"))
        result = await env.runtime.execute(
            ToolExecution(
                call_id=str(uuid.uuid4()), name="research_evidence",
                arguments={
                    "action": "adjudicate", "project_id": task_id,
                    "urls": ["https://good1.example/x"],
                },
            )
        )
        assert result.is_error is False, getattr(result.error, "message", None)
        payload = json.loads(result.content[0].text)
        assert payload["status"] == "ok" and payload["llm_calls"] == 1


# ══════════════════════════════════════════════════════════════════════════════
# Single-batch happy path: ONE LLM call, ONE commit
# ══════════════════════════════════════════════════════════════════════════════

class TestSingleBatchClosure:
    async def test_one_call_two_claims_two_sources(self, env, monkeypatch):
        svc, task_id = await _new_run(env)
        _record_claim(svc, task_id, "c1", "tomatoes are a fruit")
        _record_claim(svc, task_id, "c2", "ketchup is a smoothie")
        _install_fetch(monkeypatch)
        llm = _install_llm(
            monkeypatch,
            json.dumps({"results": [
                {"source_id": "S1", "claim_id": "c1", "verdict": "supports"},
                {"source_id": "S2", "claim_id": "c2", "verdict": "contradicts"},
            ]}),
        )
        rev_before = _proj(env, task_id)["project_revision"]
        out = await svc.adjudicate_evidence(
            USER, task_id,
            urls=["https://good1.example/a", "https://good2.example/b"],
        )
        # exactly ONE LLM round-trip for the whole closure
        assert out["status"] == "ok" and out["batches"] == 1 and out["llm_calls"] == 1
        assert len(llm.prompts) == 1
        # the prompt carries BOTH claims and BOTH short source ids (single pass)
        assert '"claim_id": "c1"' in llm.prompts[0] and '"claim_id": "c2"' in llm.prompts[0]
        assert _sids_in(llm.prompts[0]) == ["S1", "S2"]
        # snippets are deterministic representative chunks — bounded, never whole pages
        snippets = re.findall(r'"snippet": "([^"]*)"', llm.prompts[0])
        assert len(snippets) == 2 and all(
            0 < len(s) <= ADJUDICATION_SNIPPET_CHARS for s in snippets
        )
        # ONE commit: both claims applied inside the same transaction
        statuses = {i["claim_id"]: i["status"] for i in out["commit"]["items"]}
        assert statuses == {"c1": "applied", "c2": "applied"}
        assert out["commit"]["revision_after"] - rev_before >= 1
        g = _graph(env, task_id)
        assert _ticket_edges(g, "c1") == ["supports"]
        assert _ticket_edges(g, "c2") == ["contradicts"]
        # verdict summary keys are canonical URLs (explicit ids, never positions)
        assert out["per_claim"][0]["supports"] == ["https://good1.example/a"]

    async def test_insufficient_is_relevance_only_no_ticket(self, env, monkeypatch):
        svc, task_id = await _new_run(env)
        _record_claim(svc, task_id, "c1", "claim one")
        _install_fetch(monkeypatch)
        _install_llm(monkeypatch, json.dumps({"results": [
            {"source_id": "S1", "claim_id": "c1", "verdict": "insufficient"}
        ]}))
        out = await svc.adjudicate_evidence(USER, task_id, urls=["https://good1.example/a"])
        assert out["status"] == "ok"
        assert out["per_claim"][0]["insufficient"] == ["https://good1.example/a"]
        # insufficient -> neutral: no supports/contradicts ticket edge ever appears
        assert _ticket_edges(_graph(env, task_id), "c1") == []
        item = out["commit"]["items"][0]
        assert item["status"] == "applied" and item["neutral_skipped"]

    async def test_claim_ids_default_to_the_whole_graph(self, env, monkeypatch):
        svc, task_id = await _new_run(env)
        _record_claim(svc, task_id, "c1", "one")
        _record_claim(svc, task_id, "c2", "two")
        _install_fetch(monkeypatch)
        llm = _install_llm(monkeypatch, _supports("c1", "S1"))
        out = await svc.adjudicate_evidence(USER, task_id, urls=["https://good1.example/a"])
        # both claims ride the prompt; c2 simply gets no verdict row back
        assert '"claim_id": "c2"' in llm.prompts[0]
        assert out["no_verdict_claims"] == ["c2"]
        assert [i["claim_id"] for i in out["commit"]["items"]] == ["c1"]


# ══════════════════════════════════════════════════════════════════════════════
# Budget-driven splitting: multiple LLM calls, still ONE commit
# ══════════════════════════════════════════════════════════════════════════════

class TestBudgetSplitting:
    async def test_overflow_splits_sources_but_commits_once(self, env, monkeypatch):
        svc, task_id = await _new_run(env)
        _record_claim(svc, task_id, "c1", "tomatoes are a fruit")
        _install_fetch(monkeypatch)
        # squeeze the budget so each source needs its own batch (liveness: a batch
        # never closes empty)
        monkeypatch.setattr(rplugin, "ADJUDICATION_BUDGET_TOKENS", 100)

        prompts: list[str] = []

        async def dynamic(prompt, system):
            prompts.append(prompt)
            return json.dumps({"results": [
                {"source_id": s, "claim_id": "c1", "verdict": "supports"}
                for s in _sids_in(prompt)
            ]})

        monkeypatch.setattr(rplugin, "_ADJ_LLM_CALL", dynamic)
        urls = [f"https://good{i}.example/p" for i in range(1, 4)]
        out = await svc.adjudicate_evidence(USER, task_id, urls=urls)
        assert out["batches"] == 3 and out["llm_calls"] == 3
        # every batch carried the FULL claim context (claims are the fixed block)
        for p in prompts:
            assert '"claim_id": "c1"' in p
        # the source ids are partitioned across batches — never duplicated
        seen = [s for p in prompts for s in _sids_in(p)]
        assert sorted(seen) == ["S1", "S2", "S3"]
        # ONE commit for all three batches' merged verdicts
        assert len(out["commit"]["items"]) == 1
        assert sorted(out["per_claim"][0]["supports"]) == sorted(
            f"https://good{i}.example/p" for i in range(1, 4)
        )
        assert len(_ticket_edges(_graph(env, task_id), "c1")) == 3


# ══════════════════════════════════════════════════════════════════════════════
# Validation & reconciliation (server trusts ids, never positions)
# ══════════════════════════════════════════════════════════════════════════════

class TestValidation:
    async def test_hallucinated_ids_and_verdicts_are_dropped(self, env, monkeypatch):
        svc, task_id = await _new_run(env)
        _record_claim(svc, task_id, "c1", "one")
        _install_fetch(monkeypatch)
        _install_llm(monkeypatch, json.dumps({"results": [
            {"source_id": "S1", "claim_id": "c1", "verdict": "supports"},
            {"source_id": "S9", "claim_id": "c1", "verdict": "supports"},   # unknown source
            {"source_id": "S1", "claim_id": "cX", "verdict": "supports"},   # unknown claim
            {"source_id": "S1", "claim_id": "c1", "verdict": "maybe"},      # unknown verdict
            "not even an object",
        ]}))
        out = await svc.adjudicate_evidence(USER, task_id, urls=["https://good1.example/a"])
        assert out["dropped_rows"] == 4
        assert out["per_claim"][0]["supports"] == ["https://good1.example/a"]

    async def test_fenced_json_is_tolerated(self, env, monkeypatch):
        svc, task_id = await _new_run(env)
        _record_claim(svc, task_id, "c1", "one")
        _install_fetch(monkeypatch)
        _install_llm(monkeypatch, "```json\n" + _supports("c1", "S1") + "\n```")
        out = await svc.adjudicate_evidence(USER, task_id, urls=["https://good1.example/a"])
        assert out["status"] == "ok" and out["llm_calls"] == 1

    async def test_malformed_reply_gets_one_repair_then_applies(self, env, monkeypatch):
        svc, task_id = await _new_run(env)
        _record_claim(svc, task_id, "c1", "one")
        _install_fetch(monkeypatch)
        llm = _install_llm(monkeypatch, "I think the source agrees.", _supports("c1", "S1"))
        out = await svc.adjudicate_evidence(USER, task_id, urls=["https://good1.example/a"])
        assert out["llm_calls"] == 2
        assert "failed to parse" in llm.prompts[1]
        assert out["per_claim"][0]["supports"] == ["https://good1.example/a"]

    async def test_malformed_twice_fails_loudly_without_committing(self, env, monkeypatch):
        svc, task_id = await _new_run(env)
        _record_claim(svc, task_id, "c1", "one")
        _install_fetch(monkeypatch)
        _install_llm(monkeypatch, "no json here")
        rev_before = _proj(env, task_id)["project_revision"]
        with pytest.raises(RuntimeError, match="adjudication failed"):
            await svc.adjudicate_evidence(USER, task_id, urls=["https://good1.example/a"])
        # no ticket edges snuck into the graph; the claim is untouched
        assert _ticket_edges(_graph(env, task_id), "c1") == []
        assert _proj(env, task_id)["project_revision"] == rev_before


# ══════════════════════════════════════════════════════════════════════════════
# Fetch-side behavior: cap-5 internal batching + P3-7A ledger reuse
# ══════════════════════════════════════════════════════════════════════════════

class TestFetchSide:
    async def test_more_than_five_urls_batch_internally(self, env, monkeypatch):
        svc, task_id = await _new_run(env)
        _record_claim(svc, task_id, "c1", "one")
        calls: list[str] = []
        _install_fetch(monkeypatch, calls=calls)
        _install_llm(monkeypatch, _supports("c1", "S1"))
        urls = [f"https://good{i}.example/p" for i in range(1, 8)]  # 7 > FETCH_MAX_URLS
        out = await svc.adjudicate_evidence(USER, task_id, urls=urls)
        assert out["sources_used"] == 7
        # the fetch cap is an INTERNAL loop detail — the agent made one call
        assert len(out["commit"]["items"]) == 1

    async def test_already_fetched_source_adjudicates_without_refetch(self, env, monkeypatch):
        svc, task_id = await _new_run(env)
        _record_claim(svc, task_id, "c1", "one")
        calls: list[str] = []
        _install_fetch(monkeypatch, calls=calls)
        cu = "https://good1.example/recipe"
        await svc.fetch_save_batch(USER, task_id, urls=[cu])
        assert len(calls) == 1
        llm = _install_llm(monkeypatch, _supports("c1", "S1"))
        out = await svc.adjudicate_evidence(USER, task_id, urls=[cu])
        # the ref view's text came from the run's stored draft, not the network
        assert len(calls) == 1
        assert out["sources_used"] == 1
        assert "fact-good1.example" in llm.prompts[0]  # stored draft text WAS sent
        assert out["status"] == "ok"

    async def test_unfetchable_sources_skip_never_stall(self, env, monkeypatch):
        svc, task_id = await _new_run(env)
        _record_claim(svc, task_id, "c1", "one")
        _install_fetch(monkeypatch)
        llm = _install_llm(monkeypatch, _supports("c1", "S1"))
        out = await svc.adjudicate_evidence(
            USER, task_id,
            urls=["https://dead.example/x", "https://good1.example/a"],
        )
        assert [s["url"] for s in out["skipped_sources"]] == ["https://dead.example/x"]
        assert out["sources_used"] == 1
        assert _sids_in(llm.prompts[0]) == ["S1"]  # only the ONE usable source is sent

    async def test_zero_usable_sources_returns_without_any_llm_call(self, env, monkeypatch):
        svc, task_id = await _new_run(env)
        _record_claim(svc, task_id, "c1", "one")
        _install_fetch(monkeypatch)
        llm = _install_llm(monkeypatch, _supports("c1", "S1"))
        out = await svc.adjudicate_evidence(USER, task_id, urls=["https://dead.example/x"])
        assert out["status"] == "no_sources" and out["llm_calls"] == 0
        assert llm.prompts == []


# ══════════════════════════════════════════════════════════════════════════════
# Input hygiene
# ══════════════════════════════════════════════════════════════════════════════

class TestInputHygiene:
    async def test_empty_urls_rejected(self, env, monkeypatch):
        svc, task_id = await _new_run(env)
        _record_claim(svc, task_id, "c1", "one")
        with pytest.raises(ValueError, match="non-empty 'urls'"):
            await svc.adjudicate_evidence(USER, task_id, urls=[])

    async def test_unknown_claim_id_named_and_rejected(self, env, monkeypatch):
        svc, task_id = await _new_run(env)
        _record_claim(svc, task_id, "c1", "one")
        with pytest.raises(ValueError, match="unknown claim id"):
            await svc.adjudicate_evidence(
                USER, task_id, urls=["https://good1.example/a"], claim_ids=["ghost"]
            )

    async def test_no_claims_recorded_rejected(self, env, monkeypatch):
        svc, task_id = await _new_run(env)
        with pytest.raises(ValueError, match="at least one recorded Claim"):
            await svc.adjudicate_evidence(USER, task_id, urls=["https://good1.example/a"])
