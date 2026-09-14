"""Pass orchestration tests with a scripted fake LLM — no network, fully deterministic.

Verifies the frozen contracts end to end: 3 LLM calls + 1 pure Pass D, the Pass B/C
digest-subset input rule (raw source NEVER re-enters), corrective retries, and the
anti-fabrication repair loop.
"""
from __future__ import annotations

import asyncio
import json
import shutil

import pytest

from apps.api.tools.toolkit.deck import prompts as P
from apps.api.tools.toolkit.deck.errors import DeckLayoutError  # noqa: F401
from apps.api.tools.toolkit.deck.models import DeckOptions, DeckSpec
from apps.api.tools.toolkit.deck.passes import generate_deck
from apps.api.tools.toolkit.deck.render import deck_to_marp, deck_to_pptx_slides, render_deck_pdf
from tests._deck_fixtures import make_digest, make_deck

RAW_MARKER = "RAW-SOURCE-MARKER-9f3c"
SRC_TEXT = f"{RAW_MARKER} RAG 结合检索与生成,向量库按谓词隔离,成本 0.565 USD。"


class FakeLLM:
    """Queue-based scripted LLM: records every prompt/system pair it sees."""

    def __init__(self, replies):
        self.replies = list(replies)         # each entry: dict | callable(prompt)->dict | Exception
        self.calls: list[tuple[str, str]] = []

    async def complete_json(self, prompt: str, system: str) -> dict:
        self.calls.append((prompt, system))
        if not self.replies:
            raise AssertionError("FakeLLM queue exhausted")
        r = self.replies.pop(0)
        if callable(r):
            r = r(prompt)
            if asyncio.iscoroutine(r):          # async callables can script delays
                r = await r
        if isinstance(r, Exception):
            raise r
        return json.loads(json.dumps(r))     # deep copy

    async def complete(self, prompt: str, system: str) -> str:
        return json.dumps(await self.complete_json(prompt, system))

    @property
    def prompts(self):
        return [p for p, _ in self.calls]


def _src():
    from apps.api.tools.toolkit.sources import WorkspaceSource
    return [WorkspaceSource(name="doc.md", path="/ws/doc.md", text=SRC_TEXT,
                            char_count=len(SRC_TEXT), line_count=3)]


def _digest_json() -> dict:
    return make_digest().model_dump(mode="json", exclude_none=True)


def _outline_json(n=3) -> dict:
    from tests._deck_fixtures import make_outline
    return make_outline(n).model_dump(mode="json", exclude_none=True)


def _slide_json(slide_id: str) -> dict:
    if slide_id == "s2":
        return {
            "slide_id": "s2", "title": "处理流程", "key_message": "三步完成检索增强",
            "purpose": "PROCESS", "relationship": "sequential",
            "speaker_notes": "notes",
            "payload": {"steps": [{"label": "索引", "detail": "chunk 入向量库"},
                                  {"label": "检索", "detail": "top-k 召回"},
                                  {"label": "生成", "detail": "拼接上下文"}]},
            "provenance_refs": [{"source_id": "doc.md", "kind": "document",
                                 "lines": "1-3"}],
        }
    return {
        "slide_id": slide_id, "title": "单页结论", "key_message": "少做 re-verify 多复用",
        "purpose": "PROBLEM" if slide_id == "s1" else "SUMMARY",
        "relationship": "singular_takeaway", "speaker_notes": "",
        "provenance_refs": [{"source_id": "doc.md", "kind": "document", "lines": "1-2"}],
        "payload": {},
    }


def _c_reply(prompt: str) -> dict:
    for sid in ("s1", "s2", "s3"):
        if f'"{sid}"' in prompt:
            return _slide_json(sid)
    raise AssertionError("no slide id found in Pass C prompt")


def standard_replies():
    return [_digest_json(), _outline_json(), _c_reply, _c_reply, _c_reply]


def test_digest_schema_matches_model_null_optionals():
    # Real-model smoke (deepseek) sent "superseded_by": null and "page": null — legal for
    # the Pydantic models (str|None / int|None); the JSON schema must agree, or the
    # corrective-retry loop fails the pass for a shape the contract actually allows.
    from apps.api.tools.toolkit import outputs

    d = _digest_json()
    d["facts"][0]["superseded_by"] = None
    d["facts"][0]["provenance"][0]["page"] = None
    d["facts"][0]["provenance"][0]["t_ms"] = None
    assert outputs.validate(P.DIGEST_SCHEMA, d) == []


def test_digest_schema_still_rejects_invented_keys():
    from apps.api.tools.toolkit import outputs

    d = _digest_json()
    d["facts"][0]["provenance"][0]["locator"] = "3-5"
    errs = outputs.validate(P.DIGEST_SCHEMA, d)
    assert errs and any("locator" in e for e in errs)


class TestGenerateDeck:
    @pytest.mark.asyncio
    async def test_happy_path_three_llm_calls(self):
        llm = FakeLLM(standard_replies())
        deck = await generate_deck(llm, _src(), DeckOptions(target_slide_count=3))
        assert isinstance(deck, DeckSpec)
        assert [p.slide_id for p in deck.visual_plan] == \
               [s.slide_id for s in deck.slides] == ["s1", "s2", "s3"]
        assert [p.visual_type for p in deck.visual_plan] == \
               ["TEXT_HERO", "FLOWCHART", "TEXT_HERO"]
        assert len(llm.calls) == 5          # 1 digest + 1 outline + 3 slides — nothing else

    @pytest.mark.asyncio
    async def test_raw_source_never_enters_pass_b_c(self):
        # errata #1: Pass B/C must work from the digest only
        llm = FakeLLM(standard_replies())
        await generate_deck(llm, _src(), DeckOptions(target_slide_count=3))
        assert RAW_MARKER in llm.prompts[0]                 # Pass A sees raw text
        for p in llm.prompts[1:]:
            assert RAW_MARKER not in p                      # B and every C call

    @pytest.mark.asyncio
    async def test_pass_c_gets_only_referenced_facts(self):
        llm = FakeLLM(standard_replies())
        await generate_deck(llm, _src(), DeckOptions(target_slide_count=3))
        # outline fixtures reference f1/f2/f3 one per slide — each prompt holds exactly one
        for p in llm.prompts[2:]:
            cited = [f for f in ("f1", "f2", "f3") if f'"fact_id": "{f}"' in p
                     or f'"fact_id":"{f}"' in p]
            assert len(cited) == 1, p[:200]

    @pytest.mark.asyncio
    async def test_corrective_retry_on_bad_digest(self):
        bad = {"facts": [{"fact_id": "f1"}]}                # missing statement/provenance
        llm = FakeLLM([bad, *_digest_bad_retry()])
        deck = await generate_deck(llm, _src(), DeckOptions(target_slide_count=3))
        assert deck.slides
        assert "failed validation" in llm.prompts[1]

    @pytest.mark.asyncio
    async def test_oversized_digest_gets_targeted_corrective_instruction(self):
        # Incident (Agent-Harness survey run, 2026-09-14): a number-dense source made
        # deepseek emit 31..69 quantities (schema cap 30). The raw jsonschema error
        # dumped the WHOLE array repr into the retry prompt and the model never
        # converged (3 attempts → 23 min wasted). The retry must instead carry a
        # short, actionable "trim this array" instruction.
        over = _digest_json()
        q0 = over["quantities"][0]
        over["quantities"] = [dict(q0, quant_id=f"q{i}") for i in range(1, 32)]
        llm = FakeLLM([over, *_digest_bad_retry()])
        deck = await generate_deck(llm, _src(), DeckOptions(target_slide_count=3))
        assert deck.slides
        retry = llm.prompts[1]
        assert "quantities: array is too long" in retry
        assert "keep ONLY the most" in retry
        assert "q31" not in retry                      # payload dump must be gone
        assert len(retry) < 6000                       # not a 100KB error blob

    @pytest.mark.asyncio
    async def test_exhausted_too_long_retry_error_is_condensed(self):
        # The persisted job error must be human-readable: no giant array repr in it.
        over = _digest_json()
        q0 = over["quantities"][0]
        over["quantities"] = [dict(q0, quant_id=f"q{i}") for i in range(1, 32)]
        llm = FakeLLM([over, over, over])
        from apps.api.tools.toolkit.errors import GenerationError
        with pytest.raises(GenerationError) as ei:
            await generate_deck(llm, _src(), DeckOptions(target_slide_count=3))
        msg = str(ei.value)
        assert "array is too long" in msg
        assert "q31" not in msg
        assert len(msg) < 1500

    def test_digest_prompt_pins_size_caps(self):
        # Prevention: the caps must be stated up-front so attempt 1 has a chance.
        from apps.api.tools.toolkit.deck import prompts as P
        assert "facts at most 60" in P.DIGEST_SYSTEM
        assert "quantities at most 30" in P.DIGEST_SYSTEM

    @pytest.mark.asyncio
    async def test_fabricated_chart_triggers_repair_rerun(self):
        # s2 comes back as a DATA_INSIGHT citing an unknown quant → Pass D budget violation
        # → ONE corrective re-run with the violation text → fixed to steps
        bad_s2 = {
            "slide_id": "s2", "title": "成本洞察", "key_message": "成本 0.565 USD",
            "purpose": "DATA_INSIGHT", "relationship": "quantitative",
            "payload": {"series": [{"name": "cost", "points": [
                {"x": "2024", "y": 1.0, "quant_ref": "q99"},
                {"x": "2025", "y": 2.0, "quant_ref": "q99"}]}]},
            "provenance_refs": [{"source_id": "doc.md", "kind": "document", "lines": "9"}],
        }

        # queue: digest, outline, s1, s2(bad), s3, then repair for s2 (fixed).
        # Pass C forces purpose/relationship from the outline item, so the outline —
        # not the model reply — declares DATA_INSIGHT for s2.
        def c(prompt):
            if '"s1"' in prompt:
                return _slide_json("s1")
            if '"s3"' in prompt:
                return _slide_json("s3")
            if '"s2"' in prompt and "REJECTED SLIDE" not in prompt:
                return bad_s2
            return _slide_json("s2")          # repair re-run returns the clean steps slide
        o = _outline_json()
        o["sections"][0]["slides"][1] = {**o["sections"][0]["slides"][1],
                                         "purpose": "DATA_INSIGHT",
                                         "relationship": "quantitative"}
        llm = FakeLLM([_digest_json(), o, c, c, c, c])
        deck = await generate_deck(llm, _src(), DeckOptions(target_slide_count=3))
        assert deck.slides[1].payload.steps      # repaired
        assert deck.visual_plan[1].visual_type == "FLOWCHART"
        repair_prompt = next(p for p in llm.prompts if "REJECTED SLIDE" in p)
        assert "fabricated" in repair_prompt or "needs" in repair_prompt

    @pytest.mark.asyncio
    async def test_outline_budget_fail_raises_after_retries(self):
        # 3 attempts for Pass B (1 + _RETRIES) — all bad → GenerationError
        llm = FakeLLM([_digest_json()] + [{"title": "x"}] * 3)
        from apps.api.tools.toolkit.errors import GenerationError
        with pytest.raises(GenerationError):
            await generate_deck(llm, _src(), DeckOptions(target_slide_count=3))


def _digest_bad_retry():
    return [_digest_json(), _outline_json(), _c_reply, _c_reply, _c_reply]


class TestCompatExports:
    def test_marp_and_pptx_from_deck_spec(self):
        deck = make_deck([*_deck_slides()])
        md = deck_to_marp(deck)
        assert md.startswith("---") and "marp: true" in md
        assert deck.title in md
        tuples = deck_to_pptx_slides(deck)
        assert len(tuples) == len(deck.slides)
        assert all(h for h, _ in tuples)

    def test_speaker_notes_not_in_pdf_source(self):
        # errata #7: notes live in the model, never on the page
        deck = make_deck([*_deck_slides()])
        llm_deck = deck.model_copy(update={"slides": [
            s.model_copy(update={"speaker_notes": "SECRET-NOTES"}) for s in deck.slides]})
        from apps.api.tools.toolkit.deck.typst_deck import compile_deck_typst
        from apps.api.tools.toolkit.deck.layout import layout_deck
        src = compile_deck_typst(llm_deck, layout_deck(llm_deck))
        assert "SECRET-NOTES" not in src


class TestRenderPdf:
    @pytest.mark.skipif(shutil.which("typst") is None,
                        reason="typst binary only present in the worker container")
    def test_render_report_ok(self, tmp_path):
        deck = make_deck([*_deck_slides()])
        r = render_deck_pdf(deck, tmp_path)
        assert r.report.compiled, r.report.typst_warnings
        assert r.report.ok, r.report.model_dump()
        assert r.pdf and r.pdf[:5] == b"%PDF-"
        assert r.report.pages_expected == deck.pages_expected() == 1 + len(deck.slides)


def _deck_slides():
    from tests._deck_fixtures import cards_slide, flow_slide, hero_slide
    return [hero_slide(), cards_slide(4), flow_slide(4)]


# ── throughput + structural-gate contracts (2026-09-14 directive) ─────────────

def _decision_tree_outline_json() -> dict:
    """A decision-tree shaped outline: tiers, traversal process, strategy comparison, close."""
    return {
        "title": "决策树详解", "narrative_strategy": "top_down",
        "sections": [{"title": "主体", "purpose": "main", "slides": [
            {"slide_id": "s1", "title": "三层结构", "purpose": "ARCHITECTURE",
             "relationship": "hierarchical", "key_message": "根/内部/叶节点分层",
             "fact_refs": ["f1"]},
            {"slide_id": "s2", "title": "建树流程", "purpose": "PROCESS",
             "relationship": "sequential", "key_message": "四步递归分裂",
             "fact_refs": ["f2"]},
            {"slide_id": "s3", "title": "基尼 vs 信息增益", "purpose": "COMPARISON",
             "relationship": "comparative", "key_message": "两种分裂准则",
             "fact_refs": ["f3"]},
            {"slide_id": "s4", "title": "小结", "purpose": "SUMMARY",
             "relationship": "singular_takeaway", "key_message": "剪枝防过拟合",
             "fact_refs": ["f3"]},
        ]}],
    }


def _dt_c_reply(prompt: str) -> dict:
    prov = [{"source_id": "doc.md", "kind": "document", "lines": "1-3"}]
    if '"s1"' in prompt:
        return {"slide_id": "s1", "title": "三层结构", "key_message": "根/内部/叶节点分层",
                "purpose": "ARCHITECTURE", "relationship": "hierarchical",
                "speaker_notes": "", "provenance_refs": prov,
                "payload": {"items": [
                    {"label": "根节点", "detail": "全量样本", "group": "顶层"},
                    {"label": "内部节点", "detail": "特征分裂", "group": "中层"},
                    {"label": "叶节点", "detail": "类别输出", "group": "底层"}]}}
    if '"s2"' in prompt:
        return {"slide_id": "s2", "title": "建树流程", "key_message": "四步递归分裂",
                "purpose": "PROCESS", "relationship": "sequential",
                "speaker_notes": "", "provenance_refs": prov,
                "payload": {"steps": [{"label": "选择特征", "detail": "信息增益最大"},
                                      {"label": "分裂节点", "detail": "按阈值二分"},
                                      {"label": "递归", "detail": "子集继续"},
                                      {"label": "停止", "detail": "纯度达标"}]}}
    if '"s3"' in prompt:
        return {"slide_id": "s3", "title": "基尼 vs 信息增益",
                "key_message": "两种分裂准则", "purpose": "COMPARISON",
                "relationship": "comparative", "speaker_notes": "",
                "provenance_refs": prov,
                "payload": {"columns": [
                    {"header": "基尼", "cells": ["CART", "计算快"]},
                    {"header": "信息增益", "cells": ["ID3", "偏向多值"]}]}}
    return {"slide_id": "s4", "title": "小结", "key_message": "剪枝防过拟合",
            "purpose": "SUMMARY", "relationship": "singular_takeaway",
            "speaker_notes": "", "provenance_refs": prov, "payload": {}}


class TestStructuralGate:
    @pytest.mark.asyncio
    async def test_process_slide_without_steps_retries_only_that_slide(self):
        # Directive 3.2: an empty payload on a PROCESS/sequential slide is a LOUD
        # per-slide validation failure — never a silent TEXT_HERO degradation.
        calls = {"s2": 0}

        def c(prompt):
            if '"s1"' in prompt:
                return _slide_json("s1")
            if '"s3"' in prompt:
                return _slide_json("s3")
            calls["s2"] += 1
            if calls["s2"] == 1:
                bad = json.loads(json.dumps(_slide_json("s2")))
                bad["payload"] = {}                        # structured promise broken
                return bad
            return _slide_json("s2")

        llm = FakeLLM([_digest_json(), _outline_json(), c, c, c, c])
        deck = await generate_deck(llm, _src(), DeckOptions(target_slide_count=3))
        assert calls["s2"] == 2                            # only s2 retried
        assert sum(1 for p in llm.prompts[2:] if '"s1"' in p) == 1   # s1 untouched
        assert deck.slides[1].payload.steps
        assert any("requires ordered steps" in p for p in llm.prompts)

    @pytest.mark.asyncio
    async def test_purpose_and_relationship_forced_from_outline(self):
        # The model must not re-decide the slide's cognitive task to dodge the gate.
        def c(prompt):
            if '"s2"' in prompt:
                dodgy = json.loads(json.dumps(_slide_json("s2")))
                dodgy["purpose"] = "PROBLEM"
                dodgy["relationship"] = "singular_takeaway"
                return dodgy
            return _slide_json("s1" if '"s1"' in prompt else "s3")

        llm = FakeLLM([_digest_json(), _outline_json(), c, c, c])
        deck = await generate_deck(llm, _src(), DeckOptions(target_slide_count=3))
        s2 = deck.slides[1]
        assert s2.purpose == "PROCESS" and s2.relationship == "sequential"
        assert deck.visual_plan[1].visual_type == "FLOWCHART"   # no degradation

    @pytest.mark.asyncio
    async def test_slide_timeout_retries_only_that_slide(self, monkeypatch):
        from core.config import settings
        monkeypatch.setattr(settings, "deck_slide_timeout_s", 0.05)
        seen = {"s2": 0}

        async def slow_s2(prompt):
            seen["s2"] += 1
            if seen["s2"] == 1:
                await asyncio.sleep(1.0)                   # blows the 0.05s deadline
            return _slide_json("s2")

        def c(prompt):
            if '"s2"' in prompt:
                return slow_s2(prompt)                     # coroutine → FakeLLM awaits it
            return _slide_json("s1" if '"s1"' in prompt else "s3")

        llm = FakeLLM([_digest_json(), _outline_json(), c, c, c, c])
        deck = await generate_deck(llm, _src(), DeckOptions(target_slide_count=3))
        assert seen["s2"] == 2                             # timed out once, retried once
        assert deck.slides[1].payload.steps

    def test_effective_concurrency_is_min_of_caps(self, monkeypatch):
        from core.config import settings
        from apps.api.tools.toolkit.deck.passes import _effective_concurrency
        monkeypatch.setattr(settings, "deck_pass_c_concurrency", 8)
        monkeypatch.setattr(settings, "deck_provider_concurrency", 6)
        monkeypatch.setattr(settings, "deck_worker_concurrency", 8)
        assert _effective_concurrency(10) == 6             # provider binds
        assert _effective_concurrency(2) == 2              # slide_count binds
        monkeypatch.setattr(settings, "deck_provider_concurrency", 16)
        monkeypatch.setattr(settings, "deck_worker_concurrency", 16)
        assert _effective_concurrency(10) == 8             # configured binds


class TestDecisionTreeSample:
    @pytest.mark.asyncio
    async def test_visual_plan_hits_structured_types_never_all_text_hero(self):
        llm = FakeLLM([_digest_json(), _decision_tree_outline_json(),
                       _dt_c_reply, _dt_c_reply, _dt_c_reply, _dt_c_reply])
        deck = await generate_deck(llm, _src(), DeckOptions(target_slide_count=4))
        types = [p.visual_type for p in deck.visual_plan]
        assert set(types) >= {"ARCHITECTURE", "FLOWCHART", "COMPARISON"}
        assert types != ["TEXT_HERO"] * len(types)
        # structured pages were chosen by payload shape (rule1/2), never the rule4 fallback
        for p in deck.visual_plan:
            if p.visual_type in ("ARCHITECTURE", "FLOWCHART", "COMPARISON"):
                assert not p.rationale.startswith("rule4")

    @pytest.mark.skipif(shutil.which("typst") is None,
                        reason="typst binary only present in the worker container")
    def test_decision_tree_pdf_contains_vector_geometry(self, tmp_path):
        # page order: cover, ARCHITECTURE, FLOWCHART, COMPARISON, TEXT_HERO
        deck = make_deck(_dt_slides())
        r = render_deck_pdf(deck, tmp_path)
        assert r.report.ok, r.report.model_dump()
        import pymupdf
        doc = pymupdf.open(stream=r.pdf, filetype="pdf")
        try:
            counts = [len(doc[i].get_drawings()) for i in range(doc.page_count)]
        finally:
            doc.close()
        assert counts[1] >= 3 and counts[2] >= 5 and counts[3] >= 3   # real shapes
        assert counts[4] <= 2                                          # hero: text only


def _dt_slides():
    from tests._deck_fixtures import arch_slide, compare_slide, flow_slide, hero_slide
    arch = arch_slide().model_copy(update={"purpose": "ARCHITECTURE"})
    comp = compare_slide().model_copy(update={"purpose": "COMPARISON"})
    return [arch, flow_slide(4), comp, hero_slide()]
