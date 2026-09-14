"""Pass D — Visual Planning as a read-only pure function (docs §4.1, errata #3).

The selection is an ordered priority fallback chain, never a rigid matrix:

1. payload hard constraints (data shape beats everything)
2. semantic relationship (the LLM's judgement, already validated on the Slide)
3. purpose tie-break
4. safe fallback: CARDS (usable items) or TEXT_HERO — never force a drawing.

**Purity law:** these functions read a Slide and emit a VisualPlan; they MUST NOT
mutate, complete, or rewrite the semantic model. Budget problems are *reported* as
error strings so the caller can re-run Pass C with corrective feedback — the rules
never patch the slide themselves.
"""
from __future__ import annotations

from .models import (
    ContentDigest,
    LayoutIntent,
    Slide,
    VisualPlan,
    VisualType,
    text_units,
)

# Per-type budgets (docs §4.2), in mixed text_units.
BUDGETS: dict[VisualType, dict[str, int]] = {
    "TEXT_HERO":   {"key_message": 60},
    "CARDS":       {"card_label": 6, "card_detail": 25, "cards_min": 2, "cards_max": 4},
    # step_detail is calibrated to the worst-case flow node (6 steps → node_w ≈ 43mm,
    # micro 10pt ⇒ ≈10 CJK chars per line, 2 lines in the 12mm detail slot).
    "FLOWCHART":   {"steps_min": 3, "steps_max": 6, "step_label": 8, "step_detail": 18},
    "TIMELINE":    {"steps_min": 3, "steps_max": 6, "step_label": 6, "step_note": 15},
    "COMPARISON":  {"columns_max": 4, "rows_max": 6, "cell": 10},
    "ARCHITECTURE": {"tiers_max": 3, "nodes_per_tier_max": 5, "node_label": 4},
    "CHART":       {"series_max": 2, "points_max": 8},
}


# ── the pure chain ────────────────────────────────────────────────────────────

def derive_visual_plan(slide: Slide, digest: ContentDigest) -> VisualPlan:
    """Select the visual type + high-level layout intent. Read-only on both inputs."""
    p = slide.payload
    shape = p.shape()

    # 1 · payload hard constraints
    if shape == "series":
        qmap = digest.quant_map()
        refs = [pt.quant_ref for s in p.series for pt in s.points]
        if refs and all(r in qmap for r in refs) and digest.quantities:
            return _plan(slide, "CHART", f"rule1: all {len(refs)} points cite real quant_refs")
        return _plan(slide, "COMPARISON",
                     "rule1-degrade: series without resolvable quant_ref — "
                     "no fabricated chart, degrade to table")
    if shape == "columns":
        return _plan(slide, "COMPARISON", f"rule1: {len(p.columns)} comparison columns")
    if shape == "steps" and len(p.steps) >= 3:
        v: VisualType = "TIMELINE" if slide.purpose == "TIMELINE" else "FLOWCHART"
        return _plan(slide, v, f"rule1: {len(p.steps)} ordered steps, purpose={slide.purpose}")
    if shape == "items-grouped":
        return _plan(slide, "ARCHITECTURE",
                     f"rule1: {len(p.items)} tiered items "
                     f"in {len({i.group for i in p.items})} groups")

    # 2 · semantic relationship
    rel = slide.relationship
    if rel == "singular_takeaway" and shape == "message-only":
        return _plan(slide, "TEXT_HERO", "rule2: singular_takeaway, no payload shape")
    if rel == "sequential" and p.steps:
        return _plan(slide, "FLOWCHART", "rule2: sequential steps")
    if rel == "comparative":
        return _plan(slide, "COMPARISON", "rule2: comparative relationship")
    if rel == "hierarchical" and p.items:
        return _plan(slide, "ARCHITECTURE", "rule2: hierarchical items")
    if rel == "categorical" and p.items:
        return _plan(slide, "CARDS", "rule2: categorical items")
    if rel == "quantitative":
        # CHART only when real, cited data exists in the payload itself; never force it
        if digest.quantities and p.series:
            return _plan(slide, "CHART", "rule2: quantitative with real data")
        # fall through to safe fallback — no series means nothing honest to draw

    # 3 · purpose tie-break
    purpose_type: dict[str, VisualType] = {
        "PROCESS": "FLOWCHART" if p.steps else "CARDS",
        "COMPARISON": "COMPARISON" if p.columns else "CARDS",
        "TIMELINE": "TIMELINE" if p.steps else "CARDS",
        "ARCHITECTURE": "ARCHITECTURE" if p.items else "CARDS",
        "DATA_INSIGHT": "CHART" if (p.series and digest.quantities) else "COMPARISON",
    }
    if slide.purpose in purpose_type:
        return _plan(slide, purpose_type[slide.purpose], f"rule3: purpose={slide.purpose}")

    # 4 · safe fallback — never force a drawing
    if p.items and len(p.items) >= 2:
        return _plan(slide, "CARDS", "rule4: fallback to CARDS on usable items")
    return _plan(slide, "TEXT_HERO", "rule4: fallback to TEXT_HERO")


def _plan(slide: Slide, visual_type: VisualType, rationale: str) -> VisualPlan:
    return VisualPlan(
        slide_id=slide.slide_id,
        visual_type=visual_type,
        intent=derive_intent(slide, visual_type),
        rationale=rationale,
    )


def derive_intent(slide: Slide, visual_type: VisualType) -> LayoutIntent:
    """High-level intent from payload size only — never concrete geometry."""
    p = slide.payload
    n_cards = len({i.group for i in p.items}) or len(p.items)
    n_steps = len(p.steps)
    direction: str = "vertical"
    density: str = "normal"
    if visual_type in ("CARDS", "ARCHITECTURE"):
        direction = "horizontal" if n_cards >= 3 else "vertical"
        density = "compact" if n_cards >= 4 else "normal"
    elif visual_type in ("FLOWCHART", "TIMELINE"):
        direction = "horizontal"
        density = "compact" if n_steps >= 5 else "normal"
    elif visual_type == "COMPARISON":
        density = "compact" if len(p.columns) >= 4 else "normal"
        direction = "horizontal"
    elif visual_type == "TEXT_HERO":
        density = "spacious"
    emphasis = "primary" if slide.purpose in ("PROBLEM", "SUMMARY") else "neutral"
    return LayoutIntent(direction=direction, density=density, emphasis=emphasis)


# ── budget checking (reports; never mutates) ──────────────────────────────────

def budget_violations(slide: Slide, plan: VisualPlan, digest: ContentDigest) -> list[str]:
    """Type-specific budgets (docs §4.2) as error strings; [] = slide fits its plan.

    The caller feeds these into the Pass C corrective retry. This function MUST NOT
    fix anything itself (purity law).
    """
    errs: list[str] = []
    b = BUDGETS[plan.visual_type]
    p = slide.payload

    def over(label: str, units: int, cap: int, where: str) -> None:
        if units > cap:
            errs.append(f"{where}: {label} is {units} units, budget <= {cap}")

    vt = plan.visual_type
    if vt == "TEXT_HERO":
        over("key_message", text_units(slide.key_message), b["key_message"], slide.slide_id)
        if p.shape() != "message-only":
            errs.append(f"{slide.slide_id}: TEXT_HERO must carry no payload shape "
                        f"(got {p.shape()})")
    elif vt == "CARDS":
        cards = p.items
        if not b["cards_min"] <= len(cards) <= b["cards_max"]:
            errs.append(f"{slide.slide_id}: CARDS needs {b['cards_min']}..{b['cards_max']} "
                        f"cards, got {len(cards)}")
        for i, it in enumerate(cards, 1):
            over(f"card {i} label", text_units(it.label), b["card_label"], slide.slide_id)
            over(f"card {i} detail", text_units(it.detail), b["card_detail"], slide.slide_id)
    elif vt in ("FLOWCHART", "TIMELINE"):
        steps = p.steps
        if not b["steps_min"] <= len(steps) <= b["steps_max"]:
            errs.append(f"{slide.slide_id}: {vt} needs {b['steps_min']}..{b['steps_max']} "
                        f"steps, got {len(steps)}")
        for i, st in enumerate(steps, 1):
            over(f"step {i} label", text_units(st.label), b["step_label"], slide.slide_id)
            over(f"step {i} detail", text_units(st.detail),
                 b["step_detail" if vt == "FLOWCHART" else "step_note"], slide.slide_id)
    elif vt == "COMPARISON":
        cols = p.columns
        if not 2 <= len(cols) <= b["columns_max"]:
            errs.append(f"{slide.slide_id}: COMPARISON needs 2..{b['columns_max']} "
                        f"columns, got {len(cols)}")
        for c in cols:
            if len(c.cells) > b["rows_max"]:
                errs.append(f"{slide.slide_id}: column {c.header!r} has {len(c.cells)} "
                            f"rows, budget <= {b['rows_max']}")
            for j, cell in enumerate(c.cells, 1):
                over(f"{c.header!r} row {j}", text_units(cell), b["cell"], slide.slide_id)
    elif vt == "ARCHITECTURE":
        tiers = {i.group for i in p.items if i.group}
        if not 1 <= len(tiers) <= b["tiers_max"]:
            errs.append(f"{slide.slide_id}: ARCHITECTURE allows {b['tiers_max']} tiers, "
                        f"got {len(tiers)}")
        for t in sorted(tiers):
            nodes = [i for i in p.items if i.group == t]
            if len(nodes) > b["nodes_per_tier_max"]:
                errs.append(f"{slide.slide_id}: tier {t!r} has {len(nodes)} nodes, "
                            f"budget <= {b['nodes_per_tier_max']}")
            for n in nodes:
                over(f"node {n.label!r}", text_units(n.label), b["node_label"], slide.slide_id)
    elif vt == "CHART":
        qmap = digest.quant_map()
        if len(p.series) > b["series_max"]:
            errs.append(f"{slide.slide_id}: CHART allows {b['series_max']} series")
        for s in p.series:
            if len(s.points) > b["points_max"]:
                errs.append(f"{slide.slide_id}: series {s.name!r} exceeds "
                            f"{b['points_max']} points")
            for pt in s.points:
                if pt.quant_ref not in qmap:
                    errs.append(f"{slide.slide_id}: point {pt.x!r} cites unknown "
                                f"quant_ref {pt.quant_ref!r} — fabricated data forbidden")
    return errs
