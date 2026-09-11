"""Batch-1 business handlers: DISCOVER / FRAME / EVIDENCE (sealed spec).

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
import logging
from typing import Any, Awaitable, Callable

from core.infrastructure.web_fetch import canonical_url as canonicalize

from plugins.research.pipeline import (
    DegradedDecision,
    NodeCtx,
    StructuralStop,
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

    corpus_md = _render_corpus(query, keep, views, notes)
    await ctx.service.write_scratch(
        ctx.owner_id, ctx.project_id, artifact_id="corpus.md", content=corpus_md,
        idempotency_key=f"pipeline:DISCOVER:{ctx.facts.get('run_id')}:{ctx.facts.get('turn_index')}",
    )

    def _persist_corpus(p: dict) -> None:
        p.setdefault("pipeline", {})["corpus"] = {
            "query": query, "urls": keep, "channels_ran": ran,
        }
    ctx.service.atomic_update_project(ctx.owner_id, ctx.project_id, _persist_corpus)


# ═══════════════════════════════ FRAME ═══════════════════════════════════════

FRAME_SYSTEM = (
    "You frame a research task. From the corpus, produce a FALSIFIABLE core "
    "research question and the claims the research must adjudicate. Reply with "
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
        "Reply with the JSON object described in the system message."
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
