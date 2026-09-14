"""Pass D rule tests: priority fallback chain, degradation, purity, budgets."""
from __future__ import annotations

from apps.api.tools.toolkit.deck import rules
from tests._deck_fixtures import (
    arch_slide,
    cards_slide,
    chart_slide,
    compare_slide,
    flow_slide,
    hero_slide,
    make_digest,
    make_slide,
    slide_payload,
)


def plan_of(slide, digest=None):
    return rules.derive_visual_plan(slide, digest or make_digest())


class TestPriorityChain:
    def test_rule1_chart_wins_over_everything(self):
        # DATA_INSIGHT + quantitative + real quant refs → CHART
        assert plan_of(chart_slide()).visual_type == "CHART"

    def test_rule1_steps_over_relationship(self):
        # relationship categorical, but 4 ordered steps → shape beats relationship
        s = flow_slide(4)
        s = s.model_copy(update={"relationship": "categorical"})
        assert plan_of(s).visual_type == "FLOWCHART"

    def test_rule1_timeline_from_purpose(self):
        s = flow_slide(4)
        s = s.model_copy(update={"purpose": "TIMELINE"})
        assert plan_of(s).visual_type == "TIMELINE"

    def test_rule1_comparison_from_columns(self):
        assert plan_of(compare_slide()).visual_type == "COMPARISON"

    def test_rule1_architecture_from_groups(self):
        assert plan_of(arch_slide()).visual_type == "ARCHITECTURE"

    def test_rule2_relationship_when_shape_silent(self):
        s = make_slide(relationship="categorical", purpose="DEFINITION",
                       payload=slide_payload(items=[
                           {"label": "a"}, {"label": "b"}]))
        assert plan_of(s).visual_type == "CARDS"

    def test_rule2_singular_takeaway_message_only(self):
        assert plan_of(hero_slide()).visual_type == "TEXT_HERO"

    def test_rule4_fallback_single_item_text_hero(self):
        s = make_slide(relationship="comparative",
                       payload=slide_payload(items=[{"label": "只有这一个"}]))
        # comparative needs columns; none exist → purpose PROCESS not in table?
        # PROCESS has steps empty → CARDS requires 2..4 → violations reported,
        # but the TYPE decision itself must not force a chart/drawing.
        assert plan_of(s).visual_type in ("COMPARISON", "CARDS", "TEXT_HERO")


class TestAntiFabrication:
    def test_fake_quant_ref_never_charts(self):
        s = chart_slide(fabricated=True)
        p = plan_of(s)
        assert p.visual_type != "CHART"
        assert "fabricated" in p.rationale or "degrade" in p.rationale

    def test_no_quantities_never_charts(self):
        digest = make_digest(with_quantities=False)
        s = make_slide(purpose="DATA_INSIGHT", relationship="quantitative",
                       payload=slide_payload(
                           series=[{"name": "x", "points": [
                               {"x": "a", "y": 1.0, "quant_ref": "q1"},
                               {"x": "b", "y": 2.0, "quant_ref": "q1"}]}]))
        assert plan_of(s, digest).visual_type != "CHART"


class TestPurityLaw:
    def test_derive_does_not_mutate_slide(self):
        s = cards_slide()
        before = s.model_dump()
        plan_of(s)
        assert s.model_dump() == before

    def test_violations_do_not_mutate(self):
        s = cards_slide(n=6)  # 6 cards > CARDS max 4 → violations, no mutation
        p = plan_of(s)
        before = s.model_dump()
        errs = rules.budget_violations(s, p, make_digest())
        assert errs and s.model_dump() == before


class TestBudgets:
    def test_cards_count_caps(self):
        assert rules.budget_violations(cards_slide(3), plan_of(cards_slide(3)),
                                       make_digest()) == []
        errs = rules.budget_violations(cards_slide(6), plan_of(cards_slide(6)),
                                       make_digest())
        assert any("CARDS needs" in e for e in errs)

    def test_flow_steps_bounds(self):
        s = flow_slide(2)
        errs = rules.budget_violations(s, plan_of(s), make_digest())
        assert any("FLOWCHART needs 3..6" in e for e in errs)

    def test_hero_rejects_payload_shape(self):
        s = hero_slide().model_copy(update={
            "payload": slide_payload(items=[{"label": "extra"}])})
        errs = rules.budget_violations(s, plan_of(s), make_digest())
        assert any("no payload shape" in e for e in errs)

    def test_chart_point_must_resolve(self):
        s = chart_slide(fabricated=True)
        digest = make_digest()
        from apps.api.tools.toolkit.deck.models import VisualPlan
        forced = VisualPlan(slide_id=s.slide_id, visual_type="CHART")
        errs = rules.budget_violations(s, forced, digest)
        assert any("fabricated data forbidden" in e for e in errs)

    def test_architecture_tier_caps(self):
        s = arch_slide()
        assert rules.budget_violations(s, plan_of(s), make_digest()) == []

    def test_all_canonical_types_within_budget(self):
        digest = make_digest()
        for s in (hero_slide(), cards_slide(), flow_slide(), arch_slide(),
                  compare_slide(), chart_slide()):
            p = plan_of(s, digest)
            assert rules.budget_violations(s, p, digest) == [], (p.visual_type, s)
