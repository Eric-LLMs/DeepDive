"""The generic structured-LLM engine: wire-slip repair, condense, retry, stats.

Portable doctrine migrated from the retired DeckSpec passes, re-pointed at the
Brief wire shapes (``BRIEF_SCHEMAS`` + :mod:`schema`): quoted plain numbers are
coerced but ranges are NEVER forced; the ``provisionance`` typo and
``{start,end}`` locator objects are repaired mechanically; enum sentences
normalize to their unique head; optional nulls vanish; condensed errors carry
actionable advice; ``structured_call`` rides the retry loop and records honest
per-label stats; exhausted attempts fail loudly.
"""
from __future__ import annotations

import asyncio
import copy

import pytest

from apps.api.tools.toolkit.deck import prompts as P
from apps.api.tools.toolkit.deck import schema as S
from apps.api.tools.toolkit.deck import structured as ST
from apps.api.tools.toolkit.errors import GenerationError
from tests.test_deck_workflow import FakeLLM, section_payload

# ── wire-slip repair: mechanical only, never semantic ─────────────────────────

def test_quoted_numbers_coerced_ranges_never_forced():
    data = {"metrics": [
        {"name": "cost", "value": "13.7", "unit": "%",
         "locator": {"doc_id": "d", "page": 1}},
        {"name": "latency", "value": " 84 ",
         "locator": {"doc_id": "d", "page": 1}},
        {"name": "spread", "value": "15-35",
         "locator": {"doc_id": "d", "page": 1}},
    ]}
    ev: list[str] = []
    out = ST.repair_wire_slips(data, ev)
    vals = [m["value"] for m in out["metrics"]]
    assert vals == [13.7, 84, "15-35"]          # range stays a string → validation, not a fake
    assert [e for e in ev if "quoted number" in e] == [
        "quoted number value='13.7'", "quoted number value=' 84 '"]


def test_bare_value_without_a_name_is_left_alone():
    # only Metric.value (name/metric sibling) coerces; a stray quoted "value"
    # elsewhere on the wire is someone else's field.
    data = {"generation_spec": {"value": "3"}}
    out = ST.repair_wire_slips(copy.deepcopy(data))
    assert out == data


def test_provisionance_typo_and_locator_range_object_repaired():
    data = {"metrics": [{"name": "cost", "value": 1.0,
                         "provisionance": [{"doc_id": "d", "lines": {"start": 12, "end": 15}}]}],
            "locator": {"doc_id": "d", "lines": {"start": 7, "end": 7}}}
    ev: list[str] = []
    out = ST.repair_wire_slips(data, ev)
    prov = out["metrics"][0]["provenance"]
    assert "provisionance" not in out["metrics"][0]
    assert prov[0] == {"doc_id": "d", "start_line": 12, "end_line": 15}
    assert out["locator"] == {"doc_id": "d", "start_line": 7}   # single line, no end_line
    assert any("typo provisionance" in e for e in ev)
    assert sum("locator lines" in e for e in ev) == 2


def test_string_locators_rebuilt_into_objects():
    # the [doc:line] citation convention leaking back into the wire (real-run slip)
    data = {"metrics": [{"name": "latency", "value": 320, "locator": "slides.md:16"},
                        {"name": "coverage", "value": 91, "locator": "doc1:8-10"},
                        {"name": "pagebound", "value": 5, "locator": "pdf1:p3"}]}
    ev: list[str] = []
    out = ST.repair_wire_slips(data, ev)
    m = out["metrics"]
    assert m[0]["locator"] == {"doc_id": "slides.md", "start_line": 16}
    assert m[1]["locator"] == {"doc_id": "doc1", "start_line": 8, "end_line": 10}
    assert m[2]["locator"] == {"doc_id": "pdf1", "page": 3}
    assert sum("string locator" in e for e in ev) == 3


def test_locator_field_echo_string_backfilled_with_unique_doc():
    # A/text_36 survey slip: the locator schema echoed as a string
    # ("page: null, start_line: 2530"); the payload states exactly one doc id,
    # so backfilling it is unambiguous and mechanical.
    ev: list[str] = []
    data = {"evidence_refs": [{"doc_id": "survey.pdf", "start_line": 2520}],
            "metrics": [{"name": "stars", "value": 41000,
                         "locator": "page: null, start_line: 2530"},
                        {"name": "dl", "value": 14000000,
                         "locator": "start_line: 826"},
                        {"name": "p", "value": 2,
                         "locator": "page 15, line 826"}]}
    out = ST.repair_wire_slips(data, ev)
    m = out["metrics"]
    assert m[0]["locator"] == {"doc_id": "survey.pdf", "start_line": 2530}
    assert m[1]["locator"] == {"doc_id": "survey.pdf", "start_line": 826}
    assert m[2]["locator"] == {"doc_id": "survey.pdf", "page": 15, "start_line": 826}
    assert sum("string locator" in e for e in ev) == 3


def test_locator_field_echo_ambiguous_doc_not_backfilled():
    # two docs in the payload → whose line is it? refuse to guess.
    data = {"evidence_refs": [{"doc_id": "a.pdf", "start_line": 1},
                              {"doc_id": "b.pdf", "start_line": 2}],
            "metrics": [{"name": "x", "value": 1, "locator": "start_line: 826"}]}
    out = ST.repair_wire_slips(copy.deepcopy(data))
    assert out["metrics"][0]["locator"] == "start_line: 826"


def test_locator_objects_in_supporting_refs_stringified():
    # the citation-string slot got full locator objects (real survey-run slip);
    # rendering them back to "doc:8-10"/"doc:p3" preserves the content.
    ev: list[str] = []
    data = {"relationships": [
        {"source": "a", "target": "b", "supporting_refs": [
            {"doc_id": "survey.pdf", "start_line": 528, "end_line": 530},
            {"doc_id": "survey.pdf", "start_line": 7},
            {"doc_id": "survey.pdf", "page": 4},
            {"doc_id": "survey.pdf", "bbox": [0, 0, 1, 1]}]},   # unrenderable → untouched
    ]}
    out = ST.repair_wire_slips(data, ev)
    assert out["relationships"][0]["supporting_refs"] == [
        "survey.pdf:528-530", "survey.pdf:7", "survey.pdf:p4",
        {"doc_id": "survey.pdf", "bbox": [0.0, 0.0, 1.0, 1.0]}]
    assert sum("supporting_refs" in e for e in ev) == 3


def test_ingest_digest_arrays_truncated_to_wire_cap():
    assert ST._INGEST_CAPS["metrics"] == 10          # single source: prompts wire schema
    data = {"metrics": [{"name": f"m{i}", "value": i} for i in range(14)]}
    ev: list[str] = []
    out = ST.repair_wire_slips(data, ev)
    assert len(out["metrics"]) == 10
    assert out["metrics"][0]["name"] == "m0"         # model order = stated importance
    assert "metrics truncated 14->10" in ev


def test_synthesis_arrays_never_truncated():
    cards = [{"label": f"L{i}", "takeaway": f"t{i}", "epistemic_type": "FACT",
              "trace_id": "t1"} for i in range(6)]
    data = {"slides": [{"cards": cards}]}
    ev: list[str] = []
    out = ST.repair_wire_slips(data, ev)
    assert len(out["slides"][0]["cards"]) == 6       # cards fail loud, never cut
    assert ev == []


async def test_complete_json_rejects_non_object_reply():
    class ListLLM:
        async def complete_json(self, prompt, system, **kw):
            return ["not", "an", "object"]

    with pytest.raises(ST.GenerationError, match="JSON object"):
        await ST.complete_json(ListLLM(), "p", "s")


def test_plural_locator_slip_collapses_to_singular():
    # survey-run wire: nodes wrote "locators" — one element collapses to the
    # singular slot; two elements are ambiguous and must stay a hard error.
    ev: list[str] = []
    data = {"t1": {"statement": "x", "locators": [
                       {"doc_id": "a.pdf", "start_line": 4}]},
            "t2": {"statement": "y", "locators": [
                       {"doc_id": "a.pdf", "start_line": 4},
                       {"doc_id": "a.pdf", "start_line": 9}]}}
    out = ST.repair_wire_slips(data, ev)
    assert out["t1"]["locator"] == {"doc_id": "a.pdf", "start_line": 4}
    assert "locators" not in out["t1"]
    assert "locators" in out["t2"] and "locator" not in out["t2"]
    assert any("plural locators" in e for e in ev)


def test_unparseable_string_locator_left_for_validation():
    data = {"locator": "somewhere around the intro"}
    ev: list[str] = []
    out = ST.repair_wire_slips(data, ev)
    assert out["locator"] == "somewhere around the intro"
    assert ev == []


def test_null_optional_fields_stripped():
    data = {"cards": [{"label": "A", "takeaway": "t", "metric_highlight": None,
                       "trace_id": "t1", "epistemic_type": "FACT"},
                      {"label": "B", "takeaway": "u", "metric_highlight": "x"}],
            "visual_spec": {"grammar": "TIMELINE", "reuse_asset_id": None,
                            "generation_spec": None}}
    out = ST.repair_wire_slips(data)
    assert out["cards"][0] == {"label": "A", "takeaway": "t",
                               "trace_id": "t1", "epistemic_type": "FACT"}
    assert out["cards"][1]["metric_highlight"] == "x"
    assert "reuse_asset_id" not in out["visual_spec"]
    assert "generation_spec" not in out["visual_spec"]


def test_enum_head_normalized_events_recorded():
    ev: list[str] = []
    data = {"slides": [{"visual_spec": {
        "grammar": "Pipeline Flow: retrieve then generate, anchored",
        "policy": "SOURCE_FIDELITY"}},
        {"traceability": {"epistemic_type": "Fact — verified in the source table"}}]}
    out = ST.repair_wire_slips(data, ev)
    spec0 = out["slides"][0]["visual_spec"]
    assert spec0["grammar"] == "PIPELINE_FLOW"
    assert spec0["policy"] == "SOURCE_FIDELITY"          # already canonical → untouched
    assert out["slides"][1]["traceability"]["epistemic_type"] == "FACT"
    assert any("enum grammar" in e for e in ev)
    assert any("enum epistemic_type" in e for e in ev)


def test_enum_ambiguous_or_unknown_left_for_validation():
    ev: list[str] = []
    data = {"visual_spec": {"grammar": "A journey through ideas"},
            "metric": {"structure_type": "data"}}
    out = ST.repair_wire_slips(data, ev)
    assert out["visual_spec"]["grammar"] == "A journey through ideas"
    assert out["metric"]["structure_type"] == "data"
    assert ev == []
    # the helper itself: a head matching several members is ambiguous, not a guess
    assert ST._norm_enum("data", ["DATA_DASHBOARD", "DATA_CHART"]) is None


def test_prose_field_wrapped_in_object_unwrapped():
    # real-run slip: solution_approach arrived as {"description": "..."}
    ev: list[str] = []
    data = {"solution_approach": {"description": "single coder + audit protocol"},
            "problem_motivation": {"a": 1, "b": 2}}   # not a one-key string dict
    out = ST.repair_wire_slips(data, ev)
    assert out["solution_approach"] == "single coder + audit protocol"
    assert out["problem_motivation"] == {"a": 1, "b": 2}  # ambiguous → validation decides
    assert ev == ["solution_approach wrapped in object -> string"]


def test_prose_field_multi_key_string_object_joined():
    # Agent-Harness survey run slip: problem_motivation arrived as a two-part
    # {"problem": "...", "motivation": "..."}; all-string values join, mechanically.
    ev: list[str] = []
    data = {"problem_motivation": {"problem": "P sentence", "motivation": "M sentence"}}
    out = ST.repair_wire_slips(data, ev)
    assert out["problem_motivation"] == "P sentence; M sentence"
    assert ev == ["problem_motivation wrapped in object -> string"]


def test_prose_field_object_with_nested_parts_keeps_strings():
    # Agent-Harness survey run slip (2nd shape): the wrap mixes prose with nested
    # structure the block already carries in first-class fields — strings join,
    # the non-string parts drop.
    ev: list[str] = []
    data = {"solution_approach": {
        "approach": "one-sentence approach",
        "key_steps": ["step a", "step b"],
        "evidence_refs": [{"doc_id": "d", "start_line": 3}]}}
    out = ST.repair_wire_slips(data, ev)
    assert out["solution_approach"] == "one-sentence approach"
    assert ev == ["solution_approach wrapped in object -> string"]


def test_invented_relation_pruned_head_rescued_closed_untouched():
    # survey-run slip: the model invents relation labels ('supports', 'addresses');
    # the closed six are the contract — rescuable heads normalize, the rest of the
    # untrusted edge is dropped (INGEST digest, loud event).
    ev: list[str] = []
    data = {"relationships": [
        {"source": "a", "relation": "causes", "target": "b"},          # legal → untouched
        {"source": "c", "relation": "DEPENDS_ON", "target": "e"},      # head-rescued
        {"source": "f", "relation": "supports", "target": "g"},        # dropped
        {"source": "h", "relation": "addresses", "target": "i"},       # dropped
    ]}
    out = ST.repair_wire_slips(data, ev)
    assert out["relationships"] == [
        {"source": "a", "relation": "causes", "target": "b"},
        {"source": "c", "relation": "depends_on", "target": "e"}]
    assert any("enum relation" in e for e in ev)
    assert sum("relationship dropped" in e for e in ev) == 2
    # non-relationship dicts are never pruned by shape
    assert ST.repair_wire_slips({"x": {"relation": "supports"}})["x"] == {"relation": "supports"}


def test_bbox_members_become_floats():
    out = ST.repair_wire_slips({"bbox": [1, 2.5, "3", 4]})
    assert out["bbox"] == [1.0, 2.5, "3", 4.0]   # numeric members normalized; noise untouched


# ── condense_errors: actionable advice, no payload dumps ──────────────────────

def test_condense_names_the_real_caps_and_rules():
    out = ST.condense_errors([
        "slides->0->cards: [{...giant dump...}] is too long",
        "sections->2->key_elements: [1,2,3] is too long",
        "critical_metrics->1->value: '13.7' is not of type 'number'",
        "relationships->3->relation: 'supports' is not one of ['causes', 'contains']",
        "x" * 300,
    ])
    assert "HARD maximum 4" in out[0] and "merge related" in out[0]
    assert "array is too long" in out[1] and "drop the least important" in out[1]
    assert "bare JSON number" in out[2] and "never force a number" in out[2]
    assert "never invent a label" in out[3]
    assert out[4].endswith("(truncated)") and len(out[4]) < 260
    # Agent-Harness survey D/synthesize slip: FACT nodes missing a locator got a
    # bare pydantic "Value error" back — retries never converged. The advice must
    # state the locator shape, its verbatim source, and the honest fallback.
    loc = ST.condense_errors(
        ["traceability_graph->t_harness_def: Value error, FACT traceability "
         "node must carry a locator"])[0]
    assert '"doc_id"' in loc and "start_line" in loc and "page" in loc
    assert "copied VERBATIM" in loc and "never invent a locator" in loc


# ── the retry loop: repair → validate → corrective retry → loud fail ──────────

async def test_structured_call_repairs_slips_and_records_stats():
    dirty = section_payload()
    dirty["metrics"][0]["value"] = "0.565"
    del dirty["metrics"][0]["locator"]["start_line"]
    dirty["metrics"][0]["locator"]["lines"] = {"start": 3, "end": 3}
    llm = FakeLLM([dirty])
    stats: dict = {}
    loaded, _raw = await ST.structured_call(
        llm, prompt="p", system="s", schema=P.BRIEF_SCHEMAS["section"],
        extra_check=lambda d: ST.check_model(d, S.SectionUnderstanding),
        label="A/text_1", stats=stats)
    assert loaded.metrics[0].value == 0.565      # coerced before validation
    st = stats["A/text_1"]
    assert st["calls"] == 1 and st["rejected"] == 0
    assert st["prompt_tokens"] == 10 and st["completion_tokens"] == 5
    assert any("quoted number" in r for r in st["repairs"])


async def test_structured_call_condensed_retry_then_success():
    good = section_payload()
    bad = copy.deepcopy(good)
    bad["document_role"] = "A broad thematic sweep across the literature"  # unrepairable
    llm = FakeLLM([bad, good])
    stats: dict = {}
    await ST.structured_call(
        llm, prompt="ORIGINAL-PROMPT", system="s",
        schema=P.BRIEF_SCHEMAS["section"], label="A/text_9", stats=stats)
    retry = llm.calls[1]["prompt"]
    assert "ORIGINAL-PROMPT" in retry            # wrapped, not replaced
    assert "document_role" in retry              # the model sees its own slip
    assert stats["A/text_9"]["calls"] == 2 and stats["A/text_9"]["rejected"] == 1


async def test_structured_call_exhausts_loudly():
    bad = copy.deepcopy(section_payload())
    bad["section_id"] = ""                       # wire + model both reject
    llm = FakeLLM([bad, bad, bad])
    stats: dict = {}
    with pytest.raises(GenerationError, match="A/text_1 failed after 3 attempts"):
        await ST.structured_call(
            llm, prompt="p", system="s", schema=P.BRIEF_SCHEMAS["section"],
            label="A/text_1", stats=stats)
    assert stats["A/text_1"]["calls"] == 3 and stats["A/text_1"]["rejected"] == 3


async def test_structured_call_timeout_retries_in_isolation():
    good = section_payload()

    class SlowFirstLLM(FakeLLM):
        async def complete_json(self, prompt, system, timeout=None, usage_out=None,
                                images=None):
            self.calls.append({"prompt": prompt, "system": system, "images": images})
            if len(self.calls) == 1:
                await asyncio.sleep(0.5)
            return await super().complete_json(prompt, system, timeout=timeout,
                                               usage_out=usage_out, images=images)

    llm = SlowFirstLLM([good])
    stats: dict = {}
    await ST.structured_call(llm, prompt="p", system="s",
                             schema=P.BRIEF_SCHEMAS["section"], label="C/reduce",
                             timeout=0.05, stats=stats)
    st = stats["C/reduce"]
    assert st["calls"] == 2 and st["rejected"] == 1   # timed out once, retried once
    assert "timed out" in llm.calls[1]["prompt"]


# ── complete_json transport probing (fake/old gateways keep working) ──────────

class _BareLLM:
    """complete_json with an older signature (timeout only — no usage_out/images)."""

    def __init__(self, payload):
        self.payload = payload
        self.seen: list[tuple] = []

    async def complete_json(self, prompt, system, timeout=None):
        self.seen.append((prompt, system))
        return copy.deepcopy(self.payload)


async def test_images_kwarg_probed_then_dropped():
    llm = _BareLLM({"a": 1})
    out = await ST.complete_json(llm, "p", "s", images=["data:image/png;base64,x"])
    assert out == {"a": 1} and llm.seen == [("p", "s")]   # signature probed, not assumed


class _RaisingLLM:
    async def complete_json(self, prompt, system, timeout=None, usage_out=None, images=None):
        raise RuntimeError("provider refused image parts")

    async def complete(self, prompt, system, timeout=None):
        return "noise ```before\n{\"fallback\": true}"


async def test_parse_fallback_when_json_mode_breaks():
    # extract_json tolerates surrounding noise; a dead json mode is not a dead call
    out = await ST.complete_json(_RaisingLLM(), "p", "s",
                                 images=["data:image/png;base64,x"])
    assert out == {"fallback": True}


async def test_images_reach_the_transport_verbatim():
    llm = FakeLLM([section_payload()])
    await ST.complete_json(llm, "p", "s", images=["data:image/png;base64,AAA"])
    assert llm.calls[0]["images"] == ["data:image/png;base64,AAA"]
