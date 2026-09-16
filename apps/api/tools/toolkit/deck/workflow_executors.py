"""Stage executors for the presentation-brief workflow (Slides domain, core-agnostic).

Each executor is one *activity* of the definition (``understand_text`` /
``understand_visual`` / ``reduce`` / ``synthesize``). The generic runner treats it as
the black box behind the lease; everything domain-shaped — chunking, cost guards, the
structured-LLM contract, the Brief schemas — lives here and in the sibling deck
modules. Executors never touch lease mechanics: progress toward "done" is observable
state on the shared :class:`DeckRunContext`, which the adapter's probe and business
facts read at grading time.

Call bookkeeping rides the shipped stats mechanism (labels ``A/text_{i}``,
``B/visual_{asset_id}``, ``C/reduce[_g{i}]``, ``D/synthesize``).
"""
from __future__ import annotations

import asyncio
import base64
import dataclasses
import json
import mimetypes
from typing import Any

from workflow.ports import TaskRequest, TaskResult

from ..errors import GenerationError
from . import prompts as P
from . import qa as QA
from . import schema as S
from . import structured as ST


@dataclasses.dataclass
class DeckRunContext:
    """Mutable facts of one brief run; the single domain state the stages share."""

    deck_id: str
    llm: Any
    doc_rep: S.DocumentRepresentation
    controls: S.PresentationControls
    config: S.PresentationWorkflowConfig
    stats: dict = dataclasses.field(default_factory=dict)
    sections: list[S.SectionUnderstanding] = dataclasses.field(default_factory=list)
    visuals: dict[str, S.VisualUnderstanding] = dataclasses.field(default_factory=dict)
    model: S.GlobalMentalModel | None = None
    brief: S.PresentationBrief | None = None


def _dump(obj: Any) -> str:  # kept helper: model-or-dict → JSON text for prompts
    return json.dumps(
        obj.model_dump(mode="json") if hasattr(obj, "model_dump") else obj,
        ensure_ascii=False, default=str,
    )


# ── stage A: text understanding ───────────────────────────────────────────────

def _pack_chunks(ctx: DeckRunContext) -> list[dict]:
    """Greedily pack TextBlocks into conceptual chunks under the char budget.

    Deterministic (input order preserved); each chunk keeps the blocks' real
    locators so line numbers in the prompt come from the extraction, never a guess.
    """
    budget = max(500, ctx.config.section_chunk_max_chars)
    chunks: list[dict] = []
    current: list[dict] = []
    size = 0
    for block in ctx.doc_rep.text_blocks:
        item = {"text": block.text, "locator": block.locator.model_dump(mode="json")}
        n = len(item["text"])
        if current and size + n > budget:
            chunks.append({"blocks": current})
            current, size = [], 0
        current.append(item)
        size += n
    if current:
        chunks.append({"blocks": current})
    return chunks


def _section_check(expected_id: str):
    def check(data: dict) -> tuple[list[str], Any]:
        errs, model = ST.check_model(data, S.SectionUnderstanding)
        if model is not None and model.section_id != expected_id:
            errs.append(f"section_id must be {expected_id!r} (echo the id the task asks for)")
        return errs, model
    return check


class TextUnderstandExecutor:
    def __init__(self, ctx: DeckRunContext) -> None:
        self.ctx = ctx

    async def execute(self, request: TaskRequest) -> TaskResult:
        ctx = self.ctx
        chunks = _pack_chunks(ctx)
        if not chunks:
            raise GenerationError(
                "DocumentRepresentation carries no text blocks — nothing to understand")
        asset_ids = [a.asset_id for a in ctx.doc_rep.visual_assets]
        sem = asyncio.Semaphore(max(1, ctx.config.pass_concurrency))

        async def one(i: int, chunk: dict):
            async with sem:
                su, _ = await ST.structured_call(
                    ctx.llm,
                    prompt=P.section_prompt(ctx.doc_rep.document_title, _dump(chunk),
                                            _dump(asset_ids), i, ctx.controls),
                    system=P._SECTION_SYSTEM,
                    schema=P.BRIEF_SCHEMAS["section"],
                    extra_check=_section_check(f"sec_{i}"),
                    label=f"A/text_{i}",
                    call_timeout=ST.default_call_timeout(),
                    stats=ctx.stats,
                )
                return i, su

        results = await asyncio.gather(*(one(i, c) for i, c in enumerate(chunks, 1)))
        ctx.sections = [su for _, su in sorted(results, key=lambda r: r[0])]
        return TaskResult(value=f"sections={len(ctx.sections)}", spend=None)


# ── stage B: visual understanding (cost-guarded VLM) ──────────────────────────

def _bbox_size(asset: S.VisualAsset) -> tuple[float, float]:
    if not asset.bbox or len(asset.bbox) != 4:
        return (float("inf"), float("inf"))   # no bbox → nothing to reject on size
    return (abs(asset.bbox[2] - asset.bbox[0]), abs(asset.bbox[3] - asset.bbox[1]))


def _data_url(path: str) -> str:
    with open(path, "rb") as fh:
        raw = fh.read()
    mime = mimetypes.guess_type(path)[0] or "image/png"
    return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")


def _visual_check(expected_id: str):
    def check(data: dict) -> tuple[list[str], Any]:
        errs, model = ST.check_model(data, S.VisualUnderstanding)
        if model is not None and model.asset_id != expected_id:
            errs.append(f"asset_id must be {expected_id!r} (echo the id from the metadata)")
        return errs, model
    return check


class VisualUnderstandExecutor:
    def __init__(self, ctx: DeckRunContext) -> None:
        self.ctx = ctx

    async def execute(self, request: TaskRequest) -> TaskResult:
        ctx = self.ctx
        assets = ctx.doc_rep.visual_assets
        if not ctx.config.enable_visual_understanding or not assets:
            return TaskResult(value="visuals=0", spend=None)
        cfg = ctx.config
        # deterministic cost guard: largest figures first, id tiebreak; below the
        # noise floor means "never ask the VLM", it is simply not an analysis input.
        def _area(a: S.VisualAsset) -> float:
            w, h = _bbox_size(a)
            return w * h                      # no bbox → full-page fallback ranks first
        ordered = sorted(assets, key=lambda a: (-_area(a), a.asset_id))
        candidates = [
            a for a in ordered[: cfg.max_vlm_assets]
            if all(min_side >= floor for min_side, floor in
                   zip(_bbox_size(a), (cfg.min_cluster_w_pt, cfg.min_cluster_h_pt)))
        ]
        sem = asyncio.Semaphore(max(1, cfg.pass_concurrency))

        async def one(asset: S.VisualAsset):
            async with sem:
                vu, _ = await ST.structured_call(
                    ctx.llm,
                    prompt=P.visual_prompt(_dump(asset),
                                           ctx.controls),
                    system=P._VISUAL_SYSTEM,
                    schema=P.BRIEF_SCHEMAS["visual"],
                    extra_check=_visual_check(asset.asset_id),
                    label=f"B/visual_{asset.asset_id}",
                    images=[_data_url(asset.path)],
                    call_timeout=ST.default_call_timeout(),
                    stats=ctx.stats,
                )
                return asset.asset_id, vu

        results = await asyncio.gather(*(one(a) for a in candidates))
        for asset_id, vu in results:
            ctx.visuals[asset_id] = vu
        return TaskResult(value=f"visuals={len(ctx.visuals)}", spend=None)


# ── stage C: (hierarchical) reduce to the GlobalMentalModel ──────────────────

def _global_check(expected_ids: list[str]):
    def check(data: dict) -> tuple[list[str], Any]:
        errs, model = ST.check_model(data, S.GlobalMentalModel)
        if model is not None:
            got = sorted(s.section_id for s in model.sections)
            want = sorted(expected_ids)
            if got != want:
                errs.append(
                    "sections must be carried through VERBATIM and completely: "
                    f"expected {want}, got {got}")
        return errs, model
    return check


class ReduceExecutor:
    def __init__(self, ctx: DeckRunContext) -> None:
        self.ctx = ctx

    def _call(self, input_obj: dict, expected_ids: list[str], label: str):
        ctx = self.ctx
        return ST.structured_call(
            ctx.llm,
            prompt=P.reduce_prompt(_dump(input_obj), ctx.controls,
                                   ctx.doc_rep.document_title),
            system=P._REDUCE_SYSTEM,
            schema=P.BRIEF_SCHEMAS["global_model"],
            extra_check=_global_check(expected_ids),
            label=label,
            call_timeout=ST.default_call_timeout(),
            stats=ctx.stats,
        )

    async def execute(self, request: TaskRequest) -> TaskResult:
        ctx = self.ctx
        if not ctx.sections:
            raise GenerationError("reduce has no SectionUnderstandings to reduce")
        visual_index = {
            asset_id: {"presentation_worth": vu.presentation_worth.value,
                       "recommended_grammar": vu.recommended_grammar,
                       "summary": vu.visual_summary}
            for asset_id, vu in ctx.visuals.items()
        }
        section_ids = [s.section_id for s in ctx.sections]
        sections_json = [s.model_dump(mode="json") for s in ctx.sections]

        threshold = max(2, ctx.config.reduce_group_threshold)
        if ctx.config.enable_hierarchical_reduce and len(sections_json) > threshold:
            group_models = []
            for gi in range(0, len(sections_json), threshold):
                group = sections_json[gi: gi + threshold]
                gm, _ = await self._call(
                    {"document_title": ctx.doc_rep.document_title, "sections": group,
                     "visual_index": visual_index},
                    [s["section_id"] for s in group], f"C/reduce_g{gi // threshold + 1}")
                group_models.append(gm)
            merged = {
                "document_title": ctx.doc_rep.document_title,
                "group_theses": [
                    {k: getattr(gm, k) for k in
                     ("executive_thesis", "key_themes", "major_problems",
                      "major_solutions", "contradictions_and_tradeoffs")}
                    for gm in group_models],
                "sections": sections_json,
                "visual_index": visual_index,
            }
            model, _ = await self._call(merged, section_ids, "C/reduce")
        else:
            model, _ = await self._call(
                {"document_title": ctx.doc_rep.document_title,
                 "sections": sections_json, "visual_index": visual_index},
                section_ids, "C/reduce")
        model.visual_understandings = dict(ctx.visuals)
        ctx.model = model
        return TaskResult(value=f"model(sections={len(model.sections)})", spend=None)


# ── stage D: synthesis into the canonical PresentationBrief ───────────────────

def _brief_check(deck_id: str, valid_sections: set[str], known_assets: set[str]):
    return QA.make_brief_check(deck_id, valid_sections, known_assets)


class SynthesizeExecutor:
    def __init__(self, ctx: DeckRunContext) -> None:
        self.ctx = ctx

    async def execute(self, request: TaskRequest) -> TaskResult:
        ctx = self.ctx
        if ctx.model is None:
            raise GenerationError("synthesize has no GlobalMentalModel yet")
        model_json = ctx.model.model_dump(mode="json")
        model_json.pop("visual_understandings", None)
        visuals_json = {aid: vu.model_dump(mode="json") for aid, vu in ctx.visuals.items()}
        # reusable figure menu for the designer: physical assets the VLM did NOT
        # judge decorative
        decor = {aid for aid, vu in ctx.visuals.items()
                 if vu.presentation_worth == S.PresentationWorth.DECORATIVE_NOISE}
        assets_json = [
            {"asset_id": a.asset_id, "page": a.page, "type": a.type.value,
             "caption": a.semantic_hint or a.nearby_text or ""}
            for a in ctx.doc_rep.visual_assets if a.asset_id not in decor
        ]
        prompt = P.synthesis_prompt(ctx.deck_id, _dump(model_json),
                                    _dump(visuals_json), _dump(assets_json),
                                    ctx.controls)
        checker = _brief_check(
            ctx.deck_id, set(ctx.model.section_map()),
            {a.asset_id for a in ctx.doc_rep.visual_assets},
        )
        brief, _ = await ST.structured_call(
            ctx.llm,
            prompt=prompt,
            system=P.synthesis_system(),
            schema=P.BRIEF_SCHEMAS["brief"],
            extra_check=checker,
            label="D/synthesize",
            call_timeout=ST.default_call_timeout(),
            stats=ctx.stats,
        )
        # Layer-1 gates ride INSIDE the synthesize stage: "finished" on the
        # business-facts probe means "gate-clean brief", so a deck with
        # untraceable numbers never grades SUCCEEDED (§8.5).
        ctx.brief = await QA.ensure_brief_clean(
            llm=ctx.llm, deck_id=ctx.deck_id, brief=brief, controls=ctx.controls,
            model=ctx.model, doc_rep=ctx.doc_rep, stats=ctx.stats,
            synth_prompt=prompt, synth_system=P.synthesis_system())
        return TaskResult(value=f"slides={len(ctx.brief.slides)}", spend=None)
