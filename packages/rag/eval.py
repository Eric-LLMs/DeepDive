"""Golden-set regression: measure retrieval quality against expected asset hits.

A golden case pins a query to the asset ids that SHOULD surface. Because expectations are
asset-level (not chunk-level), the harness is insensitive to chunk-boundary changes from
re-chunking. Metrics are computed over the recalled assets' top-k:

- Recall@k: fraction of expected assets found in the top-k results.
- Precision@k: fraction of the top-k results that are expected.
- MRR: reciprocal rank of the first expected asset (0 if none appear).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_GOLDEN = Path("data/eval/golden.json")
DEFAULT_K = 5


@dataclass
class GoldenCase:
    id: str
    query: str
    expect_asset_ids: list[str]
    domain_id: str | None = None
    note: str = ""


@dataclass
class CaseResult:
    case_id: str
    query: str
    hit_asset_ids: list[str]
    recall_at_k: float
    precision_at_k: float
    mrr: float


@dataclass
class EvalReport:
    cases: list[CaseResult] = field(default_factory=list)
    top_k: int = DEFAULT_K

    @property
    def metrics(self) -> dict:
        if not self.cases:
            return {"n_cases": 0, "avg_recall": 0.0, "avg_precision": 0.0, "avg_mrr": 0.0}
        n = len(self.cases)
        return {
            "n_cases": n,
            "avg_recall": round(sum(c.recall_at_k for c in self.cases) / n, 4),
            "avg_precision": round(sum(c.precision_at_k for c in self.cases) / n, 4),
            "avg_mrr": round(sum(c.mrr for c in self.cases) / n, 4),
        }

    def to_dict(self) -> dict:
        return {
            "top_k": self.top_k,
            "metrics": self.metrics,
            "cases": [
                {
                    "case_id": c.case_id,
                    "query": c.query,
                    "hit_asset_ids": c.hit_asset_ids,
                    "recall@k": c.recall_at_k,
                    "precision@k": c.precision_at_k,
                    "mrr": c.mrr,
                }
                for c in self.cases
            ],
        }


def load_golden(path: str | Path | None = None) -> list[GoldenCase]:
    """Load golden cases from JSON; empty file → empty set (not an error)."""
    p = Path(path) if path else DEFAULT_GOLDEN
    if not p.exists():
        return []
    raw = json.loads(p.read_text(encoding="utf-8"))
    return [
        GoldenCase(
            id=str(c.get("id", idx)),
            query=str(c["query"]),
            expect_asset_ids=[str(a) for a in c.get("expect_asset_ids", [])],
            domain_id=c.get("domain_id"),
            note=c.get("note", ""),
        )
        for idx, c in enumerate(raw)
        if c.get("query") and c.get("expect_asset_ids")
    ]


def _asset_id(hit: dict) -> str | None:
    meta = hit.get("meta") or {}
    asset_id = meta.get("asset_id")
    return str(asset_id) if asset_id else None


def evaluate_case(case: GoldenCase, hits: list[dict], top_k: int = DEFAULT_K) -> CaseResult:
    """Score one case against the pipeline's recalled hits (top-k already applied)."""
    expected = set(case.expect_asset_ids)
    hit_asset_ids: list[str] = []
    for h in hits:
        aid = _asset_id(h)
        if aid and aid not in hit_asset_ids:
            hit_asset_ids.append(aid)

    found = [aid for aid in hit_asset_ids if aid in expected]
    rank = None
    for idx, aid in enumerate(hit_asset_ids, start=1):
        if aid in expected:
            rank = idx
            break

    recall = len(found) / len(expected) if expected else 0.0
    precision = len(found) / top_k if top_k else 0.0
    mrr = 1.0 / rank if rank is not None else 0.0
    return CaseResult(
        case_id=case.id,
        query=case.query,
        hit_asset_ids=hit_asset_ids,
        recall_at_k=round(recall, 4),
        precision_at_k=round(precision, 4),
        mrr=round(mrr, 4),
    )


async def run_golden_set(
    pipeline_factory,
    golden_path: str | None = None,
    top_k: int = DEFAULT_K,
) -> EvalReport:
    """Run every golden case through ``pipeline_factory()`` and compute metrics.

    ``pipeline_factory`` is a zero-arg callable returning a :class:`RAGPipeline` (the
    admin endpoint passes ``_rag_pipeline``; the CLI builds one from the env config).
    """
    cases = load_golden(golden_path)
    pipe = pipeline_factory()
    results: list[CaseResult] = []
    for case in cases:
        filters: dict = {}
        if case.domain_id:
            filters["domain_id"] = case.domain_id
        try:
            hits = await pipe.retrieve(case.query, top_k=top_k, filters=filters)
        except Exception:  # noqa: BLE001 - a golden case must not abort the whole run
            hits = []
        results.append(evaluate_case(case, hits, top_k))
    return EvalReport(cases=results, top_k=top_k)
