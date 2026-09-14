"""The three LLM passes + the deterministic Pass D, orchestrated into a DeckSpec.

Contract (docs §3, errata #1/#2):
  Pass A UNDERSTAND  raw source text (post budget_plan)        → ContentDigest
  Pass B OUTLINE     the digest JSON — NEVER the raw source    → Outline
  Pass C EXPANSION   per slide: outline item + the digest fact subset → Slide
  Pass D VISUAL PLAN pure :mod:`.rules` (no LLM)               → list[VisualPlan]

Every pass runs the shared validate → corrective-retry pattern: jsonschema (structure)
then Pydantic (budgets) then pure semantic checks; the concrete error strings are fed
back into one retry of the SAME prompt. Layout never trims, so budgets must be honoured
HERE — a slide that still violates after retries fails the job loudly.

Determinism: inputs are fixed by the source text + the (temperature-0 style) prompts;
the only non-determinism is the LLM itself, which the contract confines to these 3 calls.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time

from core.config import settings
from pydantic import ValidationError

from ..errors import GenerationError
from ..outputs import extract_json, validate
from ..sources import WorkspaceSource
from . import prompts as P
from .models import (
    ContentDigest,
    DeckOptions,
    DeckSpec,
    Outline,
    Slide,
    SlideOutlineItem,
    SourceDoc,
    VisualPlan,
    check_outline,
)
from .rules import budget_violations, derive_visual_plan, payload_shape_violations

logger = logging.getLogger(__name__)

_RETRIES = 2            # corrective retries per pass


def _source_docs(sources: list[WorkspaceSource]) -> list[SourceDoc]:
    out = []
    for s in sources:
        kind = "session" if s.name.endswith(".md") and "session" in s.path else "document"
        out.append(SourceDoc(source_id=s.name, kind=kind, name=s.name))
    return out


async def _complete_json(llm, prompt: str, system: str) -> dict:
    """JSON mode when available, tolerant parse otherwise (mirrors pipeline._complete_json)."""
    fn = getattr(llm, "complete_json", None)
    if fn is not None:
        try:
            return await fn(prompt, system)
        except Exception as exc:  # noqa: BLE001 - best-effort, fall back
            logger.info("complete_json unavailable (%s); falling back to parse", exc)
    raw = await llm.complete(prompt, system)
    data = extract_json(raw)
    if data is None:
        raise GenerationError("model response was not valid JSON")
    return data


def _pyd_errors(exc: ValidationError) -> list[str]:
    return [f"{'->'.join(map(str, e['loc'])) or '<root>'}: {e['msg']}"
            for e in exc.errors()]


# jsonschema renders maxItems failures as "<path>: [giant repr of the whole array] is
# too long". That payload dump pollutes the corrective retry prompt (tens of KB fed
# back to the model), bloats the persisted job error, and teaches the model nothing.
# Replace it with a short, actionable instruction; cap anything else that is huge.
_TOO_LONG_RE = re.compile(r"^(?P<path>[^:]+): \[.*\] is too long$", re.S)


def _condense_errors(errors: list[str]) -> list[str]:
    out = []
    for e in errors:
        m = _TOO_LONG_RE.match(e)
        if m:
            out.append(f"{m.group('path')}: array is too long — keep ONLY the most "
                       "decision-relevant items; drop the least important ones until "
                       "the array fits within the allowed maximum.")
        elif len(e) > 240:
            out.append(e[:240] + "… (truncated)")
        else:
            out.append(e)
    return out


async def _structured(llm, *, prompt: str, system: str, schema: dict,
                      extra_check=None, label: str,
                      timeout: float | None = None) -> tuple[object, dict]:
    """One LLM pass with corrective retries. ``extra_check(data) -> (errors, loaded)``
    runs after schema validation; returns the loaded object. ``timeout`` bounds a
    SINGLE attempt: a timed-out attempt is fed back as a corrective error so the same
    call is retried in isolation — one slow page never stalls or redoes the deck."""
    current = prompt
    last_errs: list[str] = []
    for attempt in range(_RETRIES + 1):
        try:
            if timeout:
                data = await asyncio.wait_for(_complete_json(llm, current, system), timeout)
            else:
                data = await _complete_json(llm, current, system)
        except asyncio.TimeoutError:
            errors = _condense_errors(
                [f"model response timed out after {timeout:.0f}s — produce the complete "
                 "JSON now, staying inside every stated budget"])
            last_errs = errors
            logger.info("%s pass attempt %d timed out", label, attempt + 1)
            current = P.corrective_retry_prompt(errors, prompt)
            continue
        errors = validate(schema, data)
        loaded = None
        if not errors and extra_check is not None:
            errors, loaded = extra_check(data)
        if not errors:
            return (loaded if loaded is not None else data), data
        errors = _condense_errors(errors)
        last_errs = errors
        logger.info("%s pass attempt %d rejected: %s", label, attempt + 1, errors[:3])
        current = P.corrective_retry_prompt(errors, prompt)
    raise GenerationError(f"{label} pass failed after {_RETRIES + 1} attempts: "
                          + "; ".join(last_errs[:6]))


# ── the three passes ──────────────────────────────────────────────────────────

async def pass_a_understand(llm, sources: list[WorkspaceSource],
                            hint: str = "") -> ContentDigest:
    prompt = P.digest_prompt(sources, hint)
    digest, _raw = await _structured(
        llm, prompt=prompt, system=P.DIGEST_SYSTEM, schema=P.DIGEST_SCHEMA,
        extra_check=lambda d: _check(d, ContentDigest), label="A/understand")
    return digest


async def pass_b_outline(llm, digest: ContentDigest, options: DeckOptions) -> Outline:
    digest_json = P.dumps(digest.model_dump(mode="json", exclude_none=True))
    prompt = P.outline_prompt(digest_json, options.target_slide_count,
                              options.target_audience, options.presentation_goal)

    def check(d: dict) -> tuple[list[str], object | None]:
        errs, outline = _check(d, Outline)
        if not errs:
            errs = check_outline(outline, options.target_slide_count, digest)
        return errs, outline

    outline, _raw = await _structured(
        llm, prompt=prompt, system=P.OUTLINE_SYSTEM, schema=P.OUTLINE_SCHEMA,
        extra_check=check, label="B/outline")
    return outline


def _fact_subset(digest: ContentDigest, fact_refs: list[str]) -> dict:
    """Errata #1: Pass C sees ONLY the facts its slide references (plus the quantity
    table, which is what chart points may cite) — never the raw source."""
    fmap = digest.fact_map()
    facts = [fmap[r].model_dump(mode="json", exclude_none=True)
             for r in fact_refs if r in fmap]
    # unreferenced-by-id but live facts the outline may have missed are NOT injected;
    # the outline's fact_refs are the contract for what a slide may say.
    quants = [q.model_dump(mode="json", exclude_none=True) for q in digest.quantities]
    return {"facts": facts, "quantities": quants}


def _c_check(item: SlideOutlineItem):
    """Pass C extra_check for one outline item.

    purpose/relationship are FORCED from the validated outline (the model fills content,
    it does not re-decide the slide's cognitive task), and the structural gate rejects
    slides that promise a graphic but deliver an empty/other payload — that failure
    retries ONLY this slide, closing the silent TEXT_HERO-degradation hole.
    """
    def check(d: dict) -> tuple[list[str], object | None]:
        d = dict(d)
        d.setdefault("slide_id", item.slide_id)   # the id is fixed by the outline
        d["purpose"] = item.purpose               # never model-reassignable
        d["relationship"] = item.relationship
        errs, slide = _check(d, Slide)
        if errs:
            return errs, None
        return payload_shape_violations(slide), slide
    return check


async def _pass_c_one(llm, item, section: str, subset: dict) -> Slide:
    quant_lines = "; ".join(
        f"{q['quant_id']}={q['value']}{q.get('unit', '')}"
        for q in subset["quantities"]) or ""
    prompt = P.slide_prompt(P.dumps(item.model_dump(mode="json")),
                            P.dumps(subset), section)
    slide, _raw = await _structured(
        llm, prompt=prompt, system=P.slide_system(quant_lines),
        schema=P.SLIDE_SCHEMA, extra_check=_c_check(item), label=f"C/{item.slide_id}",
        timeout=settings.deck_slide_timeout_s)
    return slide


def _effective_concurrency(n_slides: int) -> int:
    """min(configured, provider, worker, slide_count) — directive §3.1."""
    caps = (settings.deck_pass_c_concurrency, settings.deck_provider_concurrency,
            settings.deck_worker_concurrency, max(1, n_slides))
    return max(1, min(caps))


async def pass_c_expand(llm, outline: Outline, digest: ContentDigest) -> list[Slide]:
    jobs = []
    for sec in outline.sections:
        for item in sec.slides:
            jobs.append((item, sec.title, _fact_subset(digest, item.fact_refs)))
    sem = asyncio.Semaphore(_effective_concurrency(len(jobs)))

    async def one(item, section, subset):
        async with sem:
            return await _pass_c_one(llm, item, section, subset)

    results = await asyncio.gather(*(one(i, s, f) for i, s, f in jobs),
                                   return_exceptions=True)
    slides: list[Slide] = []
    for (item, _, _), res in zip(jobs, results):
        if isinstance(res, BaseException):
            raise GenerationError(f"slide {item.slide_id}: {res}") from res
        slides.append(res)
    return slides


# ── Pass D + corrective re-run (type-specific budgets only model validation can't see) ──

def pass_d_plan(slides: list[Slide], digest: ContentDigest) -> list[VisualPlan]:
    return [derive_visual_plan(s, digest) for s in slides]


async def plan_with_repair(llm, slides: list[Slide], outline: Outline,
                           digest: ContentDigest) -> tuple[list[Slide], list[VisualPlan]]:
    """Slides whose Pass D budget check reports violations get ONE corrective re-run
    with the violation strings; a still-violating slide fails the job (no silent fix)."""
    plans = pass_d_plan(slides, digest)
    bad = [(s, p) for s, p in zip(slides, plans) if budget_violations(s, p, digest)]
    if not bad:
        return slides, plans

    items = {i.slide_id: (i, sec.title) for sec in outline.sections for i in sec.slides}
    sem = asyncio.Semaphore(_effective_concurrency(len(bad)))

    async def fix(slide: Slide, plan: VisualPlan) -> Slide:
        errs = budget_violations(slide, plan, digest)
        item = items[slide.slide_id][0]
        subset = _fact_subset(digest, item.fact_refs)
        quant_lines = "; ".join(
            f"{q['quant_id']}={q['value']}{q.get('unit', '')}"
            for q in subset["quantities"]) or ""
        prompt = P.corrective_retry_prompt(
            errs,
            P.slide_prompt(P.dumps(item.model_dump(mode="json")),
                           P.dumps(subset), items[slide.slide_id][1])
            + "\n\nYOUR REJECTED SLIDE (fix the violations above, keep the meaning):\n"
            + P.dumps(slide.model_dump(mode="json", exclude_none=True)))
        async with sem:
            slide, _ = await _structured(
                llm, prompt=prompt, system=P.slide_system(quant_lines),
                schema=P.SLIDE_SCHEMA, extra_check=_c_check(item),
                label=f"C-repair/{slide.slide_id}",
                timeout=settings.deck_slide_timeout_s)
            return slide

    fixed = await asyncio.gather(*(fix(s, p) for s, p in bad), return_exceptions=True)
    repl: dict[str, Slide] = {}
    for b, f in zip(bad, fixed):
        if isinstance(f, BaseException):
            raise GenerationError(f"slide {b[0].slide_id} repair failed: {f}") from f
        repl[b[0].slide_id] = f
    slides = [repl.get(s.slide_id, s) for s in slides]
    plans = pass_d_plan(slides, digest)
    leftover = [(s.slide_id, budget_violations(s, p, digest))
                for s, p in zip(slides, plans) if budget_violations(s, p, digest)]
    if leftover:
        raise GenerationError("budget violations survive corrective retry (job fails "
                              "loudly, layout never trims): " + str(leftover[:3]))
    return slides, plans


def _check(d: dict, model_cls):
    """schema-validated dict → Pydantic load; returns (errors, model | None)."""
    try:
        return [], model_cls.model_validate(d)
    except ValidationError as exc:
        return _pyd_errors(exc), None


# ── orchestration ─────────────────────────────────────────────────────────────

async def generate_deck(llm, sources: list[WorkspaceSource],
                        options: DeckOptions | None = None, *,
                        deck_id: str = "deck", hint: str = "") -> DeckSpec:
    """Run Pass A → B → C → D and assemble the DeckSpec (the single source of truth).

    Phase timings are logged at INFO so P50/P95 can be harvested offline from worker
    logs without a separate metrics pipeline.
    """
    options = options or DeckOptions()
    t0 = time.perf_counter()
    digest = await pass_a_understand(llm, sources, hint)
    t1 = time.perf_counter()
    outline = await pass_b_outline(llm, digest, options)
    t2 = time.perf_counter()
    slides = await pass_c_expand(llm, outline, digest)
    slides, plans = await plan_with_repair(llm, slides, outline, digest)
    t3 = time.perf_counter()
    logger.info(
        "deck timing %s: A=%.1fs B=%.1fs C=%.1fs (slides=%d) total=%.1fs",
        deck_id, t1 - t0, t2 - t1, t3 - t2, len(slides), t3 - t0)
    return DeckSpec(
        deck_id=deck_id,
        title=outline.title or digest.title,
        target_audience=options.target_audience,
        presentation_goal=options.presentation_goal,
        target_slide_count=options.target_slide_count,
        narrative_strategy=outline.narrative_strategy,
        sources=_source_docs(sources),
        digest=digest,
        outline=outline,
        slides=slides,
        visual_plan=plans,
    )
