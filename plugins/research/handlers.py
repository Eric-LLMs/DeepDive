"""Batch 1+2+3 business handlers: DISCOVER … PUBLISH (sealed spec).

Batch 3 (REVIEW / REPRODUCE / PUBLISH) closes the chain:

* **REVIEW** delegates its ENTIRE LLM throughput to the service's
  ``review_draft`` closure (thinking OFF) riding this stage's gate: one
  reviewer call + one format-only repair (input = broken patch + the parse
  error, never context expansion). The patch-application physical lock
  (expected_old unique match == 1 inside the target) lives inside that
  closure. Tri-state outcome: clean → ``pass``, committed corrections →
  ``pass(after_fix)``, rejected/unparseable patch → ``review_incomplete``
  ledger line + honest advance — an UNREVIEWED edition is a quality gap,
  never a faked pass and never a pipeline blocker.
* **REPRODUCE** verifies artifact + audit integrity in PURE Python: the
  declared budget is 0 calls, so even a hypothetical straggler completion
  would hit the gate ceiling before the transport.
* **PUBLISH** is the terminal hard gate, 0 LLM: it verifies the current
  run's primary report (bound, current-edition, non-empty content) and
  promotes it. No legitimate substance → StructuralStop → BLOCKED; the
  gate never disguises an empty hand as a finished run.

One node, one honest attempt. The hard budget line each handler must honor:

* **normal path = exactly ONE semantic LLM call** (DISCOVER triage, FRAME
  decision); EVIDENCE's throughput is the adjudication closure ITSELF — the
  handler makes ZERO of its own completions, so the old "handler once +
  underlying adjudicate once" double-call is structurally impossible here.
* a failed decision may ride ONE repair pass (node total ≤ 2 calls) — never a
  third; when even the repair is invalid the outcome is either a mechanical,
  honest fallback (DISCOVER) or a StructuralStop (FRAME: no question, no run).
* everything deterministic — channel fan-out, URL dedup, fetch, chunking,
  graph writes — stays at 0 LLM calls; per-source/channel faults degrade into
  the ledger (constraint #2), they never kill the node.

Channels (web / social / rag / materials) ride ``ctx.facts`` so tests and the
worker wiring inject doubles through ``run_node(extras=...)`` — the single
sanctioned injection point. A channel that is not wired anywhere degrades
EXPLICITLY through the ledger (``source_unavailable``), never silently.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
from typing import Any, Awaitable, Callable

from core.infrastructure.web_fetch import canonical_url as canonicalize

from plugins.research.pipeline import (
    CONTRACTS,
    DegradedDecision,
    NodeCtx,
    StructuralStop,
    parse_json_reply,
    register_handler,
)

logger = logging.getLogger("research.handlers")

Channel = Callable[[str], Awaitable[list[dict]]]  # (query) -> [{url,title,text}]

CHANNEL_TIMEOUT_S = 20.0      # per-channel micro-timeout (constraint #2)
DISCOVER_MAX_SOURCES = 10     # deduped URL ceiling handed to the fetch stage
CORPUS_SNIPPET_CHARS = 900    # per-source window inside corpus.md
FRAME_CORPUS_CHARS = 6000     # corpus window handed to the framing decision
QUESTION_MIN_CHARS = 15
MAX_FRAME_CLAIMS = 12


# ── production channel defaults (tests inject doubles via run_node extras) ───

async def _web_channel(query: str) -> list[dict]:
    from core.infrastructure.web_search import get_web_search_provider

    provider = get_web_search_provider()
    if provider is None:
        raise RuntimeError("web_search provider not configured")
    res = provider.search(query, top_k=8)
    if inspect.isawaitable(res):
        res = await res
    if not isinstance(res, dict) or res.get("status") != "ok":
        err = ((res or {}).get("error") or {}) if isinstance(res, dict) else {}
        raise RuntimeError(f"web search degraded: {err.get('type', 'bad payload')}")
    items: list[dict] = []
    for r in res.get("results") or []:
        url = r.get("url") or r.get("link") or ""
        if url:
            items.append({
                "url": url,
                "title": r.get("title") or url,
                "text": r.get("content") or r.get("snippet") or "",
            })
    return items


async def _social_channel(query: str) -> list[dict]:
    from plugins.social_search.plugin import _execute

    rows = await _execute({"platform": "auto", "query": query, "limit": 8}, None)
    items: list[dict] = []
    for r in rows or []:
        if not isinstance(r, dict) or r.get("terminal_for_run"):
            continue  # platform outage marker: _execute already degraded past it
        url = r.get("url") or ""
        items.append({
            "url": url or f"social://{r.get('platform', 'unknown')}/{r.get('id', len(items))}",
            "title": r.get("title") or (r.get("content") or "")[:60],
            "text": r.get("content") or "",
        })
    return items


def _materials_channel(project: dict) -> Channel:
    async def _ch(_query: str) -> list[dict]:
        out: list[dict] = []
        for m in project.get("materials") or []:
            if isinstance(m, dict) and (m.get("url") or m.get("material_url")):
                out.append({
                    "url": m.get("url") or m["material_url"],
                    "title": m.get("name") or m.get("title") or "material",
                    "text": m.get("summary") or "",
                })
        return out
    return _ch


def _render_corpus(query: str, keep: list[str], views: dict[str, dict],
                   notes: str = "") -> str:
    parts = [f"# Research corpus: {query}", ""]
    for cu in keep:
        v = views.get(cu) or {}
        title = v.get("title") or cu
        body = (v.get("text") or "").strip()[:CORPUS_SNIPPET_CHARS]
        parts.append(f"## {title}\nSource: {cu}\n\n{body}\n")
    if notes.strip():
        parts.append(f"## Triage notes\n{notes.strip()}\n")
    return "\n".join(parts)


def _latest_artifact_text(service: Any, owner_id: Any, project_id: str,
                          artifact_id: str) -> str | None:
    """Highest-version artifact record's content (read_artifact defaults to v1)."""
    try:
        d = service._artifact_dir(owner_id, project_id, artifact_id)
        versions = sorted(
            (int(p.name[1:]) for p in d.glob("v*") if p.name[1:].isdigit()),
        )
        if not versions:
            return None
        record = service._artifact(owner_id, project_id, artifact_id, versions[-1])
    except Exception:  # noqa: BLE001 — absence is a normal branch for callers
        return None
    return record.get("content")


def _user_brief(ctx: "NodeCtx") -> str:
    """The creation-time user brief (task_spec title + description) as a prompt block.

    Loaded into every node's ctx.facts by the pipeline regardless of entry path
    (Run button / driver replay / chat), so the user's verbatim constraints —
    above all an explicit output-language instruction like 「写一个中文报告」 —
    reach every LLM decision. Empty string when the task carries no brief at all,
    which keeps prompts byte-identical to the pre-brief baseline.
    """
    spec = ctx.facts.get("task_spec") or {}
    title = str(spec.get("title") or "").strip()
    desc = str(spec.get("description") or "").strip()
    if not title and not desc:
        return ""
    return (
        "User brief (verbatim, captured at task creation):\n"
        f"Title: {title}\n"
        f"Description: {desc}\n"
        "Honor every explicit instruction in this brief. If it names an output "
        "language (e.g. 中文), all free text you generate for this task "
        "(question wording, report body, titles) must use that language; "
        "otherwise write in the language the brief/question is written in.\n\n"
    )


def _brief_dict(ctx: "NodeCtx") -> dict:
    """Same brief as a JSON-able record for structured prompt payloads."""
    spec = ctx.facts.get("task_spec") or {}
    title = str(spec.get("title") or "").strip()
    desc = str(spec.get("description") or "").strip()
    return {"title": title, "description": desc} if (title or desc) else {}


# ══════════════════════════════ DISCOVER ═════════════════════════════════════

DISCOVER_SYSTEM = (
    "You triage discovery search results for a research task. Reply with ONLY a "
    'JSON object: {"keep": ["<url>", ...], "notes": "<short triage note>"}. '
    "keep must list the URLs actually useful for the topic (a subset of the "
    "candidates); drop ads, SEO junk and off-topic hits."
)


@register_handler("DISCOVER")
async def node_discover(ctx: NodeCtx) -> None:
    query = (ctx.project.get("name") or "").strip()
    if len(query) < 3:
        raise StructuralStop(
            "DISCOVER", "topic", "project carries no research topic to seed discovery",
        )

    channels: dict[str, Channel | None] = {
        "web": ctx.facts.get("channel_web") or _web_channel,
        "social": ctx.facts.get("channel_social") or _social_channel,
        "materials": ctx.facts.get("channel_materials") or _materials_channel(ctx.project),
        "rag": ctx.facts.get("channel_rag"),  # wired by deployment or degrades explicitly
    }

    # ── 1. concurrent fan-out; every channel has its OWN micro-timeout ────────
    ran: dict[str, int] = {}
    hits: list[dict] = []

    async def _pull(name: str, fn: Channel) -> None:
        rows = await asyncio.wait_for(fn(query), timeout=CHANNEL_TIMEOUT_S)
        rows = rows or []
        ran[name] = len(rows)
        for r in rows:
            if isinstance(r, dict) and r.get("url"):
                hits.append({**r, "channel": name})

    results = await asyncio.gather(
        *(_pull(n, f) for n, f in channels.items() if f is not None),
        return_exceptions=True,
    )
    pulled = [n for n, f in channels.items() if f is not None]
    for name, res in zip(pulled, results):
        if isinstance(res, asyncio.TimeoutError):
            ctx.record(attempt=1, error_class="source_unavailable",
                       detail=f"channel {name}: exceeded {CHANNEL_TIMEOUT_S:.0f}s micro-timeout",
                       missing=name, impact="slow channel dropped; others still ran")
        elif isinstance(res, BaseException):
            ctx.record(attempt=1, error_class="source_unavailable",
                       detail=f"channel {name}: {type(res).__name__}: {res}"[:500],
                       missing=name, impact="channel outage degraded, node continues")
    if channels.get("rag") is None:
        ctx.record(attempt=1, error_class="source_unavailable",
                   detail="channel rag: not wired in this deployment",
                   missing="rag", impact="retrieval channel skipped — web/social/materials still ran")

    # ── 2. deterministic dedup + fetch (0 LLM) ────────────────────────────────
    known: dict[str, dict] = {}
    for h in hits:
        # canonicalize only folds http(s); pseudo-urls (social://) keep their raw key
        cu = canonicalize(str(h["url"])) or str(h["url"]).strip()
        if cu and cu not in known:
            known[cu] = h
    urls = list(known)[:DISCOVER_MAX_SOURCES]

    views: dict[str, dict] = {}
    for i in range(0, len(urls), 5):  # FETCH_MAX_URLS cap lives inside fetch_save_batch
        got = await ctx.service.fetch_save_batch(
            ctx.owner_id, ctx.project_id, urls=urls[i:i + 5],
        )
        for v in got or []:
            cu = v.get("canonical_url") or ""
            if not cu:
                continue
            if v.get("status") != "ok" or not (v.get("text") or "").strip():
                ctx.record(attempt=1, error_class="source_unavailable",
                           detail=f"fetch degraded: {cu} ({v.get('status') or v.get('content_status')})",
                           missing=cu, impact="source absent from the corpus")
            else:
                views[cu] = v

    # ── 2b. persist the MECHANICAL corpus before any semantic call ────────────
    # The corpus is FRAME's structural deliverable. Writing it only after triage
    # raced the node's wall-clock floor (Run-15 field lesson: the 60s wait_for
    # cancelled the handler between the triage reply and this write, so DISCOVER
    # "advanced with honest gaps" carrying no corpus at all). Persisting first
    # makes a timeout degrade to "no triage filter", never to "no corpus".
    async def _persist_corpus(keep: list[str], notes: str, key_suffix: str) -> None:
        corpus_md = _render_corpus(query, keep, views, notes)
        await ctx.service.write_scratch(
            ctx.owner_id, ctx.project_id, artifact_id="corpus.md", content=corpus_md,
            idempotency_key=(
                f"pipeline:DISCOVER:{ctx.facts.get('run_id')}:"
                f"{ctx.facts.get('turn_index')}{key_suffix}"
            ),
        )

        def _mutate(p: dict) -> None:
            p.setdefault("pipeline", {})["corpus"] = {
                "query": query, "urls": keep, "channels_ran": ran,
            }
        ctx.service.atomic_update_project(ctx.owner_id, ctx.project_id, _mutate)

    await _persist_corpus(list(views), "", "")

    # ── 3. the node's ONE semantic call: triage (repair-once at most) ────────
    notes = ""
    if views:
        candidate_block = "\n".join(
            f"- {cu} | {(v.get('title') or '')[:80]} | {(v.get('text') or '').strip()[:160]}"
            for cu, v in views.items()
        )
        prompt = (
            f"Research topic: {query}\n\nCandidate sources:\n{candidate_block}\n\n"
            "Reply with the JSON object described in the system message."
        )

        def _validate(payload: dict) -> list[str]:
            v: list[str] = []
            keep = payload.get("keep")
            if not isinstance(keep, list) or not keep:
                v.append("'keep' must be a non-empty list of candidate URLs")
            else:
                bad = [str(k) for k in keep
                       if not (canonicalize(str(k)) or str(k).strip()) in views]
                if bad:
                    v.append(f"keep cites unknown URLs: {bad[:5]} — use candidate ids verbatim")
            return v

        try:
            decision = await ctx.decide(prompt, system=DISCOVER_SYSTEM, validate=_validate)
        except DegradedDecision as exc:
            ctx.record(attempt=2, error_class="degraded_decision", detail=str(exc),
                       impact="triage skipped; mechanical corpus over every usable source")
            keep = list(views)
        else:
            keep = [canonicalize(str(k)) or str(k).strip() for k in decision.get("keep") or []
                    if (canonicalize(str(k)) or str(k).strip()) in views] or list(views)
            notes = str(decision.get("notes") or "")
    else:
        keep = []

    # Re-persist the TRIAGED corpus (dedup-safe: new version only if the
    # keep-list/notes actually changed the render).
    await _persist_corpus(keep, notes, ":triaged")


# ═══════════════════════════════ FRAME ═══════════════════════════════════════

FRAME_SYSTEM = (
    "You frame a research task. From the corpus, produce a FALSIFIABLE core "
    "research question and the claims the research must adjudicate. If the user "
    "brief names an output language, word the question (and claims) in that "
    "language. Reply with "
    'ONLY a JSON object: {"question": "...", "in_scope": "...", '
    '"out_of_scope": "...", "claims": [{"id": "k1", "statement": "...", '
    '"strength": "low|medium|high", "citations": ["<corpus URL>", ...]}]}'
)


@register_handler("FRAME")
async def node_frame(ctx: NodeCtx) -> None:
    pipe = ctx.project.get("pipeline") or {}
    corpus = pipe.get("corpus") or {}
    known = [u for u in (corpus.get("urls") or []) if isinstance(u, str)]
    corpus_md = _latest_artifact_text(
        ctx.service, ctx.owner_id, ctx.project_id, "corpus.md",
    ) or ""
    if not known or not corpus_md.strip():
        # Without a corpus there is nothing to frame against: no honest question
        # can be minted — structural, never a hallucinated seed.
        raise StructuralStop(
            "FRAME", "corpus", "DISCOVER left no usable corpus — nothing to frame against",
        )

    prompt = (
        f"Research topic: {corpus.get('query') or ctx.project.get('name') or ''}\n\n"
        f"Corpus (truncated):\n{corpus_md[:FRAME_CORPUS_CHARS]}\n\n"
        + _user_brief(ctx)
        + "Reply with the JSON object described in the system message."
    )

    def _validate(payload: dict) -> list[str]:
        q = payload.get("question")
        if not isinstance(q, str) or len(q.strip()) < QUESTION_MIN_CHARS:
            return [f"'question' must be a falsifiable research question "
                    f"of at least {QUESTION_MIN_CHARS} characters"]
        return []

    try:
        decision = await ctx.decide(prompt, system=FRAME_SYSTEM, validate=_validate)
    except DegradedDecision as exc:
        # The ONLY thing decide() can fail on here is the question itself →
        # a missing core question is the canonical structural stop.
        raise StructuralStop(
            "FRAME", "research_question",
            f"no valid research question after one repair: {exc}",
        ) from exc

    question = str(decision["question"]).strip()
    rows = decision.get("claims")
    rows = rows if isinstance(rows, list) else []
    dropped: list[str] = []
    recorded = 0
    known_set = set(known)
    q_ok = ctx.service.record_node(
        ctx.owner_id, ctx.project_id,
        node={"id": "q1", "type": "Question", "label": question[:120],
              "statement": question},
    )
    assert isinstance(q_ok, dict)  # idempotent replay returns the existing node
    for idx, row in enumerate(rows[:MAX_FRAME_CLAIMS]):
        if not isinstance(row, dict) or not isinstance(row.get("statement"), str) \
                or not row["statement"].strip():
            dropped.append(f"claim row {idx}: not an object with a statement")
            continue
        statement = row["statement"].strip()
        cits = [canonicalize(str(c)) for c in (row.get("citations") or [])
                if isinstance(c, str) and canonicalize(str(c)) in known_set]
        node = {
            "id": str(row.get("id") or f"k{idx + 1}"),
            "type": "Claim", "label": statement[:80], "statement": statement,
            "citations": cits,
        }
        strength = row.get("strength")
        if isinstance(strength, str) and strength.strip():
            node["strength"] = strength.strip().lower()
        try:
            ctx.service.record_node(ctx.owner_id, ctx.project_id, node=node)
            recorded += 1
        except Exception:  # noqa: BLE001 — bad strength etc.: retry unstyled, else drop
            node.pop("strength", None)
            try:
                ctx.service.record_node(ctx.owner_id, ctx.project_id, node=node)
                recorded += 1
            except Exception as exc:  # noqa: BLE001
                dropped.append(f"claim {node['id']}: refused by graph ({type(exc).__name__})")

    if dropped:
        def _gaps(p: dict) -> None:
            gaps = p.setdefault("pipeline", {}).setdefault("known_gaps", [])
            gaps.extend({"stage": "FRAME", "claim": d, "reason": "dropped by framing"}
                        for d in dropped)
        ctx.service.atomic_update_project(ctx.owner_id, ctx.project_id, _gaps)

    rid = ctx.facts.get("run_id")
    ti = ctx.facts.get("turn_index")
    await ctx.service.write_scratch(
        ctx.owner_id, ctx.project_id, artifact_id="research_question.md",
        content=f"# Research question\n\n{question}\n",
        idempotency_key=f"pipeline:FRAME:{rid}:{ti}:question",
    )
    await ctx.service.write_scratch(
        ctx.owner_id, ctx.project_id, artifact_id="scope.md",
        content=(f"# Scope\n\n## In scope\n{decision.get('in_scope') or ''}\n\n"
                 f"## Out of scope\n{decision.get('out_of_scope') or ''}\n"),
        idempotency_key=f"pipeline:FRAME:{rid}:{ti}:scope",
    )

    def _persist_frame(p: dict) -> None:
        p["research_question"] = question
        p.setdefault("pipeline", {})["frame"] = {
            "question": question, "claims_recorded": recorded, "claims_dropped": dropped,
        }
    ctx.service.atomic_update_project(ctx.owner_id, ctx.project_id, _persist_frame)


# ═════════════════════════════ EVIDENCE ══════════════════════════════════════

@register_handler("EVIDENCE")
async def node_evidence(ctx: NodeCtx) -> None:
    """The node's LLM throughput IS the adjudication closure — the handler makes
    zero own completions (no decide/complete here, ever). Every internal
    verdict/repair call of ``adjudicate_evidence`` rides THIS stage's gate."""
    state = ctx.service.get_state(ctx.owner_id, ctx.project_id)
    pending = [c for c in (state.get("claims") or []) if c.get("pending")]
    if not pending:
        return  # nothing owed: deterministic pass-through, 0 calls

    pipe = ctx.project.get("pipeline") or {}
    urls = [u for u in ((pipe.get("corpus") or {}).get("urls") or []) if isinstance(u, str)]
    if not urls:
        graph = ctx.service._load_graph(ctx.owner_id, ctx.project_id)
        cited = {
            canonicalize(str(c))
            for n in graph.get("nodes", [])
            if isinstance(n, dict) and n.get("type") == "Claim"
            for c in (n.get("citations") or [])
            if isinstance(c, str)
        }
        urls = sorted(cited)
    # adjudicate re-fetches: only real pages can carry verdicts
    urls = [u for u in urls if u.startswith("http")]
    if not urls:
        ctx.record(attempt=1, error_class="source_unavailable",
                   detail="evidence: no candidate URLs — neither corpus nor citations",
                   missing="sources", impact="every pending claim lands in known gaps")
        urls = []

    groups: dict[Any, list[str]] = {}
    for c in pending:
        groups.setdefault(c.get("chunk_hint") or "all", []).append(c["id"])

    no_verdict: list[str] = []
    for claim_ids in groups.values():
        if not urls:
            no_verdict.extend(claim_ids)
            continue
        res = await ctx.service.adjudicate_evidence(
            ctx.owner_id, ctx.project_id,
            urls=urls, claim_ids=claim_ids, llm_gate=ctx.gate,
        )
        for s in res.get("skipped_sources") or []:
            ctx.record(attempt=1, error_class="source_unavailable",
                       detail=f"adjudicate skipped source: {str(s)[:300]}",
                       missing=str(s.get("url") if isinstance(s, dict) else s)[:200],
                       impact="verdicts from that source absent; claims may go known-gap")
        no_verdict.extend(res.get("no_verdict_claims") or [])
        # A semantic gap is honest Known-Gaps material, not a retry: every claim
        # left without a supports/contradicts ticket (pure "insufficient" or no
        # verdict at all) is recorded here — never re-adjudicated in this node.
        for row in res.get("per_claim") or []:
            if isinstance(row, dict) and row.get("claim_id") \
                    and not (row.get("supports") or row.get("contradicts")):
                no_verdict.append(row["claim_id"])

    if no_verdict:
        def _gaps(p: dict) -> None:
            gaps = p.setdefault("pipeline", {}).setdefault("known_gaps", [])
            existing = {g.get("claim_id") for g in gaps if isinstance(g, dict)}
            gaps.extend({"stage": "EVIDENCE", "claim_id": cid,
                         "reason": "adjudicated, no verdict — insufficient evidence "
                                   "(a gap, never a refutation)"}
                        for cid in sorted(set(no_verdict)) if cid not in existing)
        ctx.service.atomic_update_project(ctx.owner_id, ctx.project_id, _gaps)


# ═════════════════════════════ DESIGN ════════════════════════════════════════

DESIGN_SYSTEM = (
    "You design the method for a research task. Reply with ONLY a JSON object: "
    '{"method": "<concrete approach>", "steps": ["<ordered analysis steps>", '
    '...], "data_needed": ["..."], "success_criteria": "<how we know the '
    'question is answered>", "register": "<which observations count as '
    'evidence, and at what grain>", "estimand": "<the conclusion this design '
    'targets>", "identification": "<the rule that ties each claim to its '
    'evidence>", "risk": "<the main threat to validity>"}'
)
DESIGN_MIN_METHOD = 20
# The four epistemic fields DESIGN_GATE's mechanical check reads off the graph
# Design node; they are part of the decision contract so the gate can never
# guard a transition the pipeline itself cannot satisfy.
DESIGN_CONTRACT_FIELDS = (
    ("register", "which observations count as evidence, and at what grain"),
    ("estimand", "the conclusion this design targets"),
    ("identification", "the rule that ties each claim to its evidence"),
    ("risk", "the main threat to validity"),
)
DESIGN_CONTRACT_MIN = 10


@register_handler("DESIGN")
async def node_design(ctx: NodeCtx) -> None:
    """Python prepares the template; ONE decision call (repair-once at most)."""
    question = (ctx.project.get("research_question") or "").strip()
    state = ctx.service.get_state(ctx.owner_id, ctx.project_id)
    claims = state.get("claims") or []
    if not question:
        raise StructuralStop("DESIGN", "research_question",
                             "FRAME left no research question to design against")

    template = (
        f"Research question: {question}\n"
        f"Claims recorded: {len(claims)} "
        f"(pending verification: {sum(1 for c in claims if c.get('pending'))})\n"
        "Known gaps so far: "
        f"{[g.get('claim_id') or g.get('claim') for g in (ctx.project.get('pipeline') or {}).get('known_gaps') or []][:10]}\n"
    )

    def _validate(p: dict) -> list[str]:
        v: list[str] = []
        m = p.get("method")
        if not isinstance(m, str) or len(m.strip()) < DESIGN_MIN_METHOD:
            v.append(f"'method' must describe the approach (≥{DESIGN_MIN_METHOD} chars)")
        s = p.get("steps")
        if not isinstance(s, list) or not s or not all(
                isinstance(x, str) and x.strip() for x in s):
            v.append("'steps' must be a non-empty list of step strings")
        sc = p.get("success_criteria")
        if not isinstance(sc, str) or not sc.strip():
            v.append("'success_criteria' is required")
        for f, hint in DESIGN_CONTRACT_FIELDS:
            val = p.get(f)
            if not isinstance(val, str) or len(val.strip()) < DESIGN_CONTRACT_MIN:
                v.append(f"'{f}' must be a string (≥{DESIGN_CONTRACT_MIN} chars): {hint}")
        return v

    prompt = (
        template + _user_brief(ctx) + "\nDesign the analysis method for this question. "
        "Reply with the JSON object described in the system message."
    )
    try:
        decision = await ctx.decide(prompt, system=DESIGN_SYSTEM, validate=_validate)
    except DegradedDecision as exc:
        # DESIGN is degradable: mechanical fallback plan from the claim chunks,
        # ledger the failure honestly, advance. The fallback carries ALL EIGHT
        # schema fields with static, gate-satisfying text (≥10 chars each) — the
        # degraded path must never fail its own contract.
        ctx.record(attempt=2, error_class="degraded_decision", detail=str(exc),
                   impact="mechanical generic design used")
        decision = {
            "method": "Adjudicate every recorded claim against the corpus and "
                      "aggregate verdicts per claim.",
            "steps": ["aggregate claim verdict statistics",
                      "build the evidence table", "report citation coverage",
                      "list unresolved gaps"],
            "data_needed": ["corpus", "claim graph"],
            "success_criteria": "every claim carries a verdict or an honest gap",
            "register": "Recorded corpus passages, one claim verdict per claim.",
            "estimand": "Aggregated verdict statistics as the answer form.",
            "identification": "Claims adjudicate only against cited corpus passages.",
            "risk": "Corpus coverage gaps and single-source unsupported claims.",
        }

    # The gate's epistemic four fields, normalized once for node + artifact + state.
    design_contract = {
        f: str(decision[f]).strip() for f, _hint in DESIGN_CONTRACT_FIELDS
    }
    ctx.service.record_node(
        ctx.owner_id, ctx.project_id,
        node={"id": "design", "type": "Design",
              "label": design_contract["estimand"][:80], **design_contract},
    )

    body = (
        f"# Research design\n\n## Method\n{decision['method'].strip()}\n\n"
        "## Steps\n" + "".join(f"- {s}\n" for s in decision["steps"])
        + f"\n## Data needed\n{', '.join(map(str, decision.get('data_needed') or []))}\n\n"
        f"## Success criteria\n{decision['success_criteria'].strip()}\n\n"
        "## Design contract\n"
        + "".join(f"- {f.title()}: {design_contract[f]}\n" for f in design_contract)
    )
    await ctx.service.write_scratch(
        ctx.owner_id, ctx.project_id, artifact_id="design.md", content=body,
        idempotency_key=f"pipeline:DESIGN:{ctx.facts.get('run_id')}:{ctx.facts.get('turn_index')}",
    )

    def _persist(p: dict) -> None:
        p.setdefault("pipeline", {})["design"] = {
            "method": decision["method"], "steps": decision["steps"],
            **design_contract,
        }
    ctx.service.atomic_update_project(ctx.owner_id, ctx.project_id, _persist)


# ═════════════════════════════ EXECUTE (two-beat, hard ceiling) ══════════════
#
# Beat 1 = Action Plan, Beat 2 = Summary: the stage's FULL declared budget.
# Each beat is ONE bare ``ctx.complete`` — never ``decide()`` (a repair pass
# would eat the other beat's budget). No third call is possible: the stage
# gate (max_calls=2) power-cuts any hidden extra completion into the ledger.
# Between the beats everything is Python: whitelisted deterministic steps,
# each wrapped in the record_execution/finish_execution audit chain, 0 LLM.

EXECUTE_PLAN_SYSTEM = (
    "You plan deterministic analysis steps for the EXECUTE stage. Reply with "
    'ONLY a JSON object: {"steps": [{"op": "<op name>", "args": {}}]} — valid '
    "ops are listed in the prompt; any other op is dropped. No LLM-using steps "
    "may be planned: this stage runs Python analysis only."
)
EXECUTE_SUMMARY_SYSTEM = (
    "You summarize executed analysis results for the research record. Reply "
    'with ONLY a JSON object: {"summary": "<dense factual summary>"}.'
)


def _claim_stats(graph: dict) -> dict:
    claims = [n for n in graph.get("nodes", [])
              if isinstance(n, dict) and n.get("type") == "Claim"]
    tickets: dict[str, list[str]] = {}
    for e in graph.get("edges", []):
        if isinstance(e, dict) and e.get("kind") in ("supports", "contradicts"):
            tickets.setdefault(e.get("src"), []).append(e["kind"])
    return {
        "claims": len(claims),
        "with_supports": sum(1 for c in claims if "supports" in tickets.get(c["id"], [])),
        "with_contradicts": sum(1 for c in claims if "contradicts" in tickets.get(c["id"], [])),
        "no_ticket": sum(1 for c in claims if not tickets.get(c["id"])),
    }


def _evidence_table(graph: dict) -> dict:
    ev = {n.get("id"): n for n in graph.get("nodes", [])
          if isinstance(n, dict) and n.get("type") == "Evidence"}
    rows = []
    for e in graph.get("edges", []):
        if isinstance(e, dict) and e.get("kind") in ("supports", "contradicts"):
            node = ev.get(e.get("dst")) or {}
            rows.append({"claim": e.get("src"), "kind": e["kind"],
                         "verdict": node.get("verdict") or "",
                         "url": node.get("url") or ""})
    return {"rows": rows[:50], "total": len(rows)}


def _coverage_report(service: Any, ctx: NodeCtx) -> dict:
    state = service.get_state(ctx.owner_id, ctx.project_id)
    claims = state.get("claims") or []
    return {
        "anchored": sum(1 for c in claims if c.get("anchored")),
        "unanchored": [c["id"] for c in claims if not c.get("anchored")][:20],
        "gapped": [c["id"] for c in claims if c.get("gap")][:20],
    }


def _gap_list(ctx: NodeCtx) -> dict:
    gaps = (ctx.project.get("pipeline") or {}).get("known_gaps") or []
    return {"known_gaps": gaps[:30], "total": len(gaps)}


@register_handler("EXECUTE")
async def node_execute(ctx: NodeCtx) -> None:
    rid, ti = ctx.facts.get("run_id"), ctx.facts.get("turn_index")
    graph = ctx.service._load_graph(ctx.owner_id, ctx.project_id)
    ops: dict[str, Callable[[], dict]] = {
        "claim_stats": lambda: _claim_stats(graph),
        "evidence_table": lambda: _evidence_table(graph),
        "coverage_report": lambda: _coverage_report(ctx.service, ctx),
        "gap_list": lambda: _gap_list(ctx),
    }

    # ── beat 1: Action Plan (ONE bare completion, no repair budget exists) ───
    prompt = (
        f"Question: {ctx.project.get('research_question') or ''}\n"
        f"Design steps: {(ctx.project.get('pipeline') or {}).get('design', {}).get('steps') or []}\n"
        f"Valid ops with their meaning: {json.dumps({k: 'python analysis snapshot' for k in ops})}\n"
        "Plan the deterministic execution steps.\n\n"
        + _user_brief(ctx)
    )
    raw = await ctx.complete(prompt, system=EXECUTE_PLAN_SYSTEM)
    try:
        plan = parse_json_reply(raw)
        assert isinstance(plan.get("steps"), list)
    except (ValueError, AssertionError) as exc:
        ctx.record(attempt=1, error_class="degraded_decision",
                   detail=f"invalid action plan (no repair allowed — budget): {exc}"[:500],
                   impact="default full op sweep executed instead")
        plan = {"steps": [{"op": k} for k in ops]}

    # ── middle: deterministic execution + audit chain (0 LLM) ────────────────
    outputs: dict[str, dict] = {}
    skipped: list[str] = []
    for i, step in enumerate(plan.get("steps") or []):
        op = step.get("op") if isinstance(step, dict) else None
        if op not in ops:
            skipped.append(str(op))
            continue
        exec_id = f"{rid}:{ti}:exec:{op}:{i}"
        ctx.service.record_execution(ctx.owner_id, ctx.project_id,
                                     tool=f"pipeline.execute.{op}", args=dict(step.get("args") or {}),
                                     execution_id=exec_id)
        try:
            result = ops[op]()
            ctx.service.finish_execution(ctx.owner_id, ctx.project_id,
                                         execution_id=exec_id, result={"status": "ok", **result})
            outputs[op] = result
        except Exception as exc:  # noqa: BLE001 — a dead step is a ledger line, not a dead node
            ctx.service.finish_execution(ctx.owner_id, ctx.project_id,
                                         execution_id=exec_id,
                                         result={"status": "error", "error": f"{type(exc).__name__}: {exc}"[:300]})
            ctx.record(attempt=1, error_class="handler_error",
                       detail=f"execute step {op}: {type(exc).__name__}: {exc}"[:500],
                       impact=f"analysis output {op} missing from the record")
    if skipped:
        ctx.record(attempt=1, error_class="degraded_decision",
                   detail=f"plan steps outside the deterministic whitelist dropped: {skipped[:6]}"[:500],
                   impact="unplanned work absent from this stage's outputs")

    # ── beat 2: Summary (the SECOND and LAST completion of this node) ────────
    summary = ""
    raw2 = await ctx.complete(
        "Execution outputs:\n" + json.dumps(outputs, ensure_ascii=False)[:4000]
        + "\nSummarize what this execution established for the research record.",
        system=EXECUTE_SUMMARY_SYSTEM,
    )
    try:
        s = parse_json_reply(raw2)
        summary = str(s.get("summary") or "").strip()
    except ValueError as exc:
        ctx.record(attempt=2, error_class="degraded_decision",
                   detail=f"invalid summary payload: {exc}"[:500],
                   impact="raw summary text used verbatim")
        summary = raw2.strip()[:600]

    await ctx.service.write_scratch(
        ctx.owner_id, ctx.project_id, artifact_id="execution_notes.md",
        # NOT named like a report on purpose: "report" in the stem would let this
        # EXECUTE-stage artifact win the first-write primary binding and leave
        # WRITE's actual paper (report.md) unbound, unreviewed and unpromoted.
        content="# Execution notes\n\n## Summary\n" + summary
                + "\n\n## Outputs\n```json\n"
                + json.dumps(outputs, ensure_ascii=False, indent=1)[:6000] + "\n```\n",
        idempotency_key=f"pipeline:EXECUTE:{rid}:{ti}",
    )

    def _persist(p: dict) -> None:
        p.setdefault("pipeline", {})["execution"] = {
            "outputs": outputs, "summary": summary, "skipped_steps": skipped,
        }
    ctx.service.atomic_update_project(ctx.owner_id, ctx.project_id, _persist)


# ═════════════════════════════ EXPLAIN ═══════════════════════════════════════

EXPLAIN_SYSTEM = (
    "You derive causal explanations from adjudicated evidence. Reply with ONLY "
    'a JSON object: {"explanations": [{"claim_id": "<existing claim id>", '
    '"causal_line": "<mechanism from evidence to claim>", "confidence": '
    '"low|medium|high"}], "open_questions": ["<question the evidence cannot '
    'answer>"]}'
)


@register_handler("EXPLAIN")
async def node_explain(ctx: NodeCtx) -> None:
    graph = ctx.service._load_graph(ctx.owner_id, ctx.project_id)
    claim_ids = {n.get("id") for n in graph.get("nodes", [])
                 if isinstance(n, dict) and n.get("type") == "Claim"}
    if not claim_ids:
        raise StructuralStop("EXPLAIN", "claims",
                             "no claims to explain — EVIDENCE left the graph empty")

    def _validate(p: dict) -> list[str]:
        v: list[str] = []
        ex = p.get("explanations")
        if not isinstance(ex, list):
            v.append("'explanations' must be a list")
        else:
            bad = [i for i, r in enumerate(ex)
                   if not (isinstance(r, dict) and isinstance(r.get("claim_id"), str)
                           and isinstance(r.get("causal_line"), str)
                           and r["causal_line"].strip())]
            if bad:
                v.append(f"explanation rows {bad[:5]} need claim_id + non-empty causal_line")
        if not isinstance(p.get("open_questions"), list):
            v.append("'open_questions' must be a list")
        return v

    evidence_rows = [
        {"claim": e.get("src"), "kind": e.get("kind"),
         "verdict": ({n.get("id"): n for n in graph.get("nodes", [])
                      if isinstance(n, dict)}.get(e.get("dst")) or {}).get("verdict", "")}
        for e in graph.get("edges", [])
        if isinstance(e, dict) and e.get("kind") in ("supports", "contradicts")
    ][:40]
    prompt = (
        f"Question: {ctx.project.get('research_question') or ''}\n"
        f"Claim ids: {sorted(claim_ids)}\n"
        f"Verdict tickets: {json.dumps(evidence_rows, ensure_ascii=False)}\n"
        f"Execution summary: {((ctx.project.get('pipeline') or {}).get('execution') or {}).get('summary', '')[:800]}\n"
        "Explain the causal lines. Reply with the JSON object in the system message.\n\n"
        + _user_brief(ctx)
    )
    decision = await ctx.decide(prompt, system=EXPLAIN_SYSTEM, validate=_validate)

    kept: list[dict] = []
    ghost: list[str] = []
    for row in decision.get("explanations") or []:
        if row.get("claim_id") in claim_ids:
            kept.append(row)
        else:
            ghost.append(str(row.get("claim_id")))
    opens = [str(q) for q in decision.get("open_questions") or [] if str(q).strip()]

    if ghost or opens:
        def _gaps(p: dict) -> None:
            gaps = p.setdefault("pipeline", {}).setdefault("known_gaps", [])
            gaps.extend({"stage": "EXPLAIN", "claim_id": g,
                         "reason": "explanation referenced a claim that does not exist"}
                        for g in sorted(set(ghost)))
            gaps.extend({"stage": "EXPLAIN", "question": q,
                         "reason": "open question — evidence cannot settle it"}
                        for q in opens)
        ctx.service.atomic_update_project(ctx.owner_id, ctx.project_id, _gaps)

    body = (
        f"# Causal explanations\n\nQuestion: {ctx.project.get('research_question') or ''}\n\n"
        + "".join(f"## {r['claim_id']} ({r.get('confidence', '?')})\n{r['causal_line']}\n\n"
                  for r in kept)
        + ("## Open questions\n" + "".join(f"- {q}\n" for q in opens) if opens else "")
    )
    await ctx.service.write_scratch(
        ctx.owner_id, ctx.project_id, artifact_id="explain.md", content=body,
        idempotency_key=f"pipeline:EXPLAIN:{ctx.facts.get('run_id')}:{ctx.facts.get('turn_index')}",
    )

    def _persist(p: dict) -> None:
        p.setdefault("pipeline", {})["explain"] = {
            "explanations": kept, "open_questions": opens, "ghost_claims": sorted(set(ghost)),
        }
    ctx.service.atomic_update_project(ctx.owner_id, ctx.project_id, _persist)


# ═════════════════════════════ WRITE (Thinking ON, hard gate) ════════════════

WRITE_SYSTEM = (
    "You write the research draft. Using ONLY the supplied evidence record, "
    "produce a structured markdown report that answers the research question, "
    "states every verdict and every known gap honestly. Write the title and "
    "body in the output language the user brief demands (e.g. 中文 for "
    "「写一个中文报告」); keep URLs and citations verbatim. Reply with ONLY a JSON "
    'object: {"title": "...", "md": "<full markdown draft with >=3 ## '
    'sections; cite sources inline>"}'
)
WRITE_MIN_CHARS = 800


@register_handler("WRITE")
async def node_write(ctx: NodeCtx) -> None:
    """The ONLY thinking-on generation node. Two failed drafts is a STRUCTURAL
    fatal: without an honest draft the run cannot complete — terminal BLOCKED,
    never a force-advance of an empty stage."""
    question = (ctx.project.get("research_question") or "").strip()
    graph = ctx.service._load_graph(ctx.owner_id, ctx.project_id)
    pipe = ctx.project.get("pipeline") or {}
    if not question:
        raise StructuralStop("WRITE", "research_question",
                             "nothing to write against — the question is absent")

    def _validate(p: dict) -> list[str]:
        v: list[str] = []
        md = p.get("md")
        if not isinstance(md, str) or len(md.strip()) < WRITE_MIN_CHARS:
            v.append(f"'md' must be a full draft of at least {WRITE_MIN_CHARS} characters")
        elif md.count("\n## ") < 3:
            v.append("the draft needs at least 3 '## ' sections")
        return v

    record = {
        "question": question,
        "user_brief": _brief_dict(ctx),
        "claims": [
            {"id": n.get("id"), "statement": n.get("statement") or n.get("label"),
             "citations": n.get("citations") or [],
             "gap": (n.get("gap") or {}).get("status") if isinstance(n.get("gap"), dict) else None}
            for n in graph.get("nodes", [])
            if isinstance(n, dict) and n.get("type") == "Claim"
        ][:60],
        "known_gaps": pipe.get("known_gaps") or [],
        "execution_summary": (pipe.get("execution") or {}).get("summary", "")[:1200],
    }
    prompt = (
        "Evidence record (authoritative, do not invent claims):\n"
        + json.dumps(record, ensure_ascii=False)[:12000]
        + "\n\n" + _user_brief(ctx)
        + f"Write the draft answering: {question}"
    )
    try:
        decision = await ctx.decide(prompt, system=WRITE_SYSTEM, validate=_validate)
    except DegradedDecision as exc:
        raise StructuralStop(
            "WRITE", "draft",
            f"no compliant draft after one repair — {exc}",
        ) from exc

    md = decision["md"].strip()
    title = str(decision.get("title") or question)[:120]
    from plugins.research.plugin import get_auto_run_fence  # audited producer identity
    # report-named artifact: the first write binds primary_report_artifact_id (T3),
    # so the WRITE->REVIEW G1 physical check is satisfied by THIS landing — its
    # version stamping and cloud mirroring stay 100% Python (0 LLM).
    await ctx.service.write_scratch(
        ctx.owner_id, ctx.project_id, artifact_id="report.md",
        content=f"# {title}\n\n{md}\n",
        idempotency_key=f"pipeline:WRITE:{ctx.facts.get('run_id')}:{ctx.facts.get('turn_index')}",
        generated_by_execution=(get_auto_run_fence() or {}).get("execution_id"),
    )
    ctx.service.record_node(
        ctx.owner_id, ctx.project_id,
        node={"id": "draft-1", "type": "Draft", "label": title,
              "artifact": "report.md"},
    )

    def _persist(p: dict) -> None:
        p.setdefault("pipeline", {})["write"] = {
            "artifact": "report.md", "title": title, "chars": len(md),
        }
    ctx.service.atomic_update_project(ctx.owner_id, ctx.project_id, _persist)


# ═════════════════════════════ REVIEW (thinking OFF, tri-state + physical lock) ═


def _render_scorecard(ctx: NodeCtx) -> str:
    """Mechanical QUALITY_GATE scorecard — 0 LLM, deterministic from persisted facts.

    Format contract (``plugin._parse_scorecard_rows``): pipe table, first cell an
    integer, FOURTH column the Fatal flag. Exactly 8 dimension rows always satisfy
    the >=7 bar. Blocking severity lives ONLY in Fatal 'yes' and is reserved for
    DISQUALIFYING absences (no verified source, no claim, no/empty report) —
    ordinary quality gaps (unreviewed edition, anchoring < 100%) score low with
    Fatal 'no' so a flaw never blocks the chain: that is the gate's own
    fatal==0 verdict, aligned with the strict park semantics.
    """
    project = ctx.service.read_project(ctx.owner_id, ctx.project_id)
    graph = ctx.service._load_graph(ctx.owner_id, ctx.project_id)
    pipe = project.get("pipeline") or {}
    nodes = [n for n in graph.get("nodes") or [] if isinstance(n, dict)]
    by_type = lambda t: [n for n in nodes if n.get("type") == t]  # noqa: E731
    sources, claims = by_type("Source"), by_type("Claim")
    verified = [s for s in sources if s.get("verification_status") == "verified"]
    anchored = [c for c in claims if c.get("citations") and c.get("strength")]
    tickets: dict[str, list[str]] = {}
    for e in graph.get("edges") or []:
        if isinstance(e, dict) and e.get("kind") in ("supports", "contradicts"):
            tickets.setdefault(str(e.get("dst")), []).append(e["kind"])
    covered = [c for c in claims if tickets.get(str(c.get("id")))]
    urls = (pipe.get("corpus") or {}).get("urls") or []
    gaps = pipe.get("known_gaps") or []
    ledger = pipe.get("failure_ledger") or []
    review = pipe.get("review") or {}

    report_ok = False
    primary = (project.get("primary_report_artifact_id") or "").strip()
    if primary:
        try:
            d = ctx.service._artifact_dir(ctx.owner_id, ctx.project_id, primary)
            vs = sorted(int(p.name[1:]) for p in d.glob("v*") if p.name[1:].isdigit())
            rec = (ctx.service._artifact(ctx.owner_id, ctx.project_id, primary, vs[-1])
                   if vs else None)
            report_ok = bool(rec and (rec.get("content") or "").strip())
        except Exception:  # noqa: BLE001 — absence is a fatal row, never a crash
            report_ok = False

    def _cell(s: object) -> str:
        return str(s).replace("|", "/").strip()

    rows = [
        ("Corpus assembled for adjudication", f"{len(urls)} urls", False,
         f"known gaps recorded: {len(gaps)}"),
        ("Evidence sources verified", f"{len(verified)}/{len(sources)}",
         len(verified) == 0, "every Source must carry verification_status=verified"),
        ("Claims recorded", f"{len(claims)}", len(claims) == 0,
         "claims frame the research question"),
        ("Claims anchored (citations + strength)", f"{len(anchored)}/{len(claims)}",
         False, "a gap when <n/n; disclosed, never hidden"),
        ("Claims with verdict or honest gap", f"{len(covered)}/{len(claims)}",
         False, f"verdict tickets + {len(gaps)} gap entries"),
        ("Primary report bound and non-empty", "yes" if report_ok else "no",
         not report_ok, "the deliverable CLAIM_GATE hard-stops on"),
        ("Edition reviewed", review.get("status", "missing"), False,
         _cell(review.get("verdict") or "no review verdict")),
        ("Failure ledger disclosed", f"{len(ledger)} lines", False,
         "degradations surface in the report's honest-gap section"),
    ]
    lines = [
        "# Quality scorecard",
        "",
        "Mechanical (0-LLM): computed from persisted graph/ledger state at REVIEW "
        "close.", "Blocking is Fatal only; quality gaps score low, never fatal.",
        "",
        "| # | Criterion | Score | Fatal | Note |",
        "|---|---|---|---|---|",
    ]
    lines += [
        f"| {i} | {_cell(c)} | {_cell(sc)} | {'yes' if ftl else 'no'} | {_cell(nt)} |"
        for i, (c, sc, ftl, nt) in enumerate(rows, 1)
    ]
    return "\n".join(lines) + "\n"


@register_handler("REVIEW")
async def node_review(ctx: NodeCtx) -> None:
    """Zero own completions: the node's ENTIRE LLM throughput is the service's
    ``review_draft`` closure (reviewer + single format-only repair), riding this
    stage's gate. The unique-match patch lock (expected_old count == 1 inside
    the target) is enforced inside that closure — all-or-nothing staging.

    Tri-state verdict, mapped honestly:
      1. no corrections                -> ``pass``
      2. corrections applied & committed -> ``pass(after_fix)``
      3. patch rejected / invalid twice -> ``review_incomplete``: a
         ``degraded_decision`` ledger line + honest advance (the published
         edition is UNREVIEWED — constraint #3 forbids faking a pass, and
         an unreviewed report is a quality gap, not a structural absence).
    """
    primary = (ctx.project.get("primary_report_artifact_id") or "").strip()
    if not primary:
        # No deliverable at all by the time REVIEW opens: G1 physically blocks
        # WRITE->REVIEW, so reaching here means state corruption, not a quality
        # gap — there is nothing an honest advance could carry forward.
        raise StructuralStop("REVIEW", "report", "no primary report bound to review")

    try:
        res = await ctx.service.review_draft(
            ctx.owner_id, ctx.project_id, artifact_id=primary, llm_gate=ctx.gate,
        )
    except RuntimeError as exc:
        # review_draft's honest "nothing was written" outcome: rejected patch
        # rows or two invalid replies. Degrade + advance; never retry the stage.
        ctx.record(attempt=2, error_class="degraded_decision",
                   detail=f"review_incomplete: {exc}"[:500],
                   impact="report stays UNREVIEWED; published edition carries no "
                          "reviewer corrections")
        status, verdict = "unreviewed", "review_incomplete"
        new_version = None
        changes = 0
        res: dict = {}
    else:
        new_version = res.get("new_version")
        changes = int(res.get("changes_applied") or 0)
        if new_version:
            status, verdict = "pass_after_fix", f"pass(after_fix): v{new_version}"
        else:
            status, verdict = "pass", "pass: no corrections required"

    def _persist(p: dict) -> None:
        p.setdefault("pipeline", {})["review"] = {
            "status": status, "verdict": verdict, "artifact": primary,
            "base_version": res.get("base_version"),
            "new_version": new_version, "changes_applied": changes,
            "llm_calls": CONTRACTS["REVIEW"].llm_calls - ctx.gate.remaining,
        }
    ctx.service.atomic_update_project(ctx.owner_id, ctx.project_id, _persist)

    # ── QUALITY_GATE feed: mechanical scorecard (0 LLM) ──────────────────────
    # write_scratch stamps the current run_seq (G3), the exact provenance the
    # gate's per-edition scope demands. A render failure is a ledger line — the
    # gate then sees scorecard_missing, parks on FAIL (strict), never crashes.
    try:
        await ctx.service.write_scratch(
            ctx.owner_id, ctx.project_id, artifact_id="scorecard.md",
            content=_render_scorecard(ctx),
            idempotency_key=(
                f"pipeline:REVIEW:{ctx.facts.get('run_id')}:"
                f"{ctx.facts.get('turn_index')}:scorecard"
            ),
        )
    except Exception as exc:  # noqa: BLE001 — honest gap, handled by the gate
        ctx.record(attempt=1, error_class="handler_error",
                   detail=f"scorecard render failed: {type(exc).__name__}: {exc}"[:500],
                   impact="QUALITY_GATE has no scorecard for this edition")


# ═════════════════════════════ REPRODUCE (pure Python, 0 LLM declared) ═════════

@register_handler("REPRODUCE")
async def node_reproduce(ctx: NodeCtx) -> None:
    """Artifact + audit integrity verification — 100% Python. The declared
    budget is ZERO calls: the stage gate would cut off any hypothetical
    completion before the transport, and this handler never even touches it."""
    project = ctx.service.read_project(ctx.owner_id, ctx.project_id)
    pipe = project.get("pipeline") or {}
    primary = (project.get("primary_report_artifact_id") or "").strip()

    checks: list[dict] = []

    def _check(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail[:300]})
        if not ok:
            ctx.record(attempt=1, error_class="handler_error",
                       detail=f"integrity: {name}: {detail}"[:500],
                       impact=f"reproducibility record incomplete at {name}")

    # 1. the deliverable exists and belongs to THIS run edition (G3 tag).
    record: dict | None = None
    if primary:
        try:
            d = ctx.service._artifact_dir(ctx.owner_id, ctx.project_id, primary)
            versions = sorted(
                (int(p.name[1:]) for p in d.glob("v*") if p.name[1:].isdigit()),
            )
            if versions:
                record = ctx.service._artifact(
                    ctx.owner_id, ctx.project_id, primary, versions[-1],
                )
        except Exception:  # noqa: BLE001 — absence is a failed check, not a crash
            record = None
    _check("primary_report_bound", bool(primary), f"primary={primary!r}")
    _check("report_version_on_disk", record is not None,
           "no artifact version found" if record is None else "")
    _check("report_edition_matches",
           record is not None and not ctx.service._is_ghost_run_seq(
               record.get("run_seq"), project.get("run_seq")),
           "" if record is not None else "no record to compare")
    _check("report_content_non_empty",
           bool((record or {}).get("content", "").strip()),
           "latest version carries empty content")

    # 2. the audit chain finished: no execution row left RUNNING mid-flight.
    execs = ctx.service._load_json(
        ctx.service._project_dir(ctx.owner_id, ctx.project_id) / "executions.json",
        {"executions": []},
    ).get("executions") or []
    unfinished = [
        r.get("tool", "?") for r in execs
        if isinstance(r, dict) and str(r.get("status") or "").upper() == "RUNNING"
    ]
    _check("audit_chain_settled", not unfinished,
           f"running executions: {unfinished[:5]}")

    # 3. the REVIEW verdict was honestly recorded (pass or unreviewed — either
    #    way an attempt exists; a silent skip is the integrity fault).
    review = pipe.get("review") or {}
    _check("review_attempt_recorded", bool(review.get("status")),
           "pipeline.review missing — REVIEW never reported a verdict")

    body = (
        "# Reproduction record\n\n"
        "Deterministic integrity audit of the research chain (0 LLM).\n\n"
        + "".join(
            f"- [{'x' if c['ok'] else ' '}] {c['name']}"
            + (f" — {c['detail']}" if c["detail"] and not c["ok"] else "") + "\n"
            for c in checks
        )
        + f"\nExecutions audited: {len(execs)}\n"
        + f"Known gaps carried: {len(pipe.get('known_gaps') or [])}\n"
        + f"Failure ledger lines: {len(pipe.get('failure_ledger') or [])}\n"
    )
    await ctx.service.write_scratch(
        ctx.owner_id, ctx.project_id, artifact_id="reproduce.md", content=body,
        idempotency_key=f"pipeline:REPRODUCE:{ctx.facts.get('run_id')}:{ctx.facts.get('turn_index')}",
    )

    def _persist(p: dict) -> None:
        p.setdefault("pipeline", {})["reproduce"] = {
            "checks": checks, "executions": len(execs),
        }
    ctx.service.atomic_update_project(ctx.owner_id, ctx.project_id, _persist)


# ═════════════════════════════ PUBLISH (terminal hard gate, 0 LLM) ══════════════

@register_handler("PUBLISH")
async def node_publish(ctx: NodeCtx) -> None:
    """The absolute terminal gate (constraint #3): pure Python verification of
    the run's substance, then the drive promotion. A legitimate, promotable
    primary report -> promoted (the framework then leaves the chain finished);
    anything less -> StructuralStop -> BLOCKED. Missing substance is NEVER
    disguised — the gate's whole purpose is that an empty hand cannot pass."""
    project = ctx.service.read_project(ctx.owner_id, ctx.project_id)
    primary = (project.get("primary_report_artifact_id") or "").strip()

    def _stop(detail: str) -> StructuralStop:
        return StructuralStop("PUBLISH", "promoted_artifact", detail)

    if not primary:
        raise _stop("no primary report bound — nothing was ever written to publish")

    record: dict | None = None
    try:
        d = ctx.service._artifact_dir(ctx.owner_id, ctx.project_id, primary)
        versions = sorted(
            (int(p.name[1:]) for p in d.glob("v*") if p.name[1:].isdigit()),
        )
        if versions:
            record = ctx.service._artifact(ctx.owner_id, ctx.project_id, primary, versions[-1])
    except Exception:  # noqa: BLE001 — a missing tree is a stop, not a crash
        record = None
    if record is None or not (record.get("content") or "").strip():
        raise _stop(f"'{primary}' has no version with real content on disk")
    if ctx.service._is_ghost_run_seq(record.get("run_seq"), project.get("run_seq")):
        raise _stop(
            f"'{primary}' v{record.get('version')} is a ghost (run_seq="
            f"{record.get('run_seq')} != {project.get('run_seq')}) — a stale "
            "cross-edition report must never be published as this run's output"
        )

    try:
        view = await ctx.service.promote_to_drive(
            ctx.owner_id, ctx.project_id, artifact_id=primary,
            promote_idempotency_key=f"research:{ctx.project_id}:{primary}:{record.get('version')}",
        )
    except ValueError as exc:
        # promote_to_drive's honest refusals (no content / ghost / drive-side
        # pre-check): still no promotable substance — terminal, never disguised.
        raise _stop(f"promotion refused: {exc}") from exc

    if view.get("status") != "PROMOTED" or not view.get("drive_asset_id"):
        raise _stop(
            f"promotion returned no promoted identity (status={view.get('status')!r}, "
            f"drive_asset_id={view.get('drive_asset_id')!r})"
        )

    def _persist(p: dict) -> None:
        p.setdefault("pipeline", {})["publish"] = {
            "status": "PROMOTED",
            "artifact": view.get("artifact_id"),
            "version": view.get("version"),
            "drive_asset_id": view.get("drive_asset_id"),
            "drive_path": view.get("drive_path"),
            "rag_status": view.get("rag_status"),
            "idempotent": bool(view.get("idempotent")),
        }
    ctx.service.atomic_update_project(ctx.owner_id, ctx.project_id, _persist)
