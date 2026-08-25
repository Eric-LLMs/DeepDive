"""CLI for the golden-set RAG regression.

Usage:
    python scripts/eval_rag.py [--golden data/eval/golden.json] [--top-k 5] [--out data/eval/results]

Builds the pipeline from the env-seeded config (or a stored ``app_settings["rag"]``
config if one exists), runs every golden case, prints a metrics table, and writes the
report JSON to ``data/eval/results/<timestamp>.json``.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from core.config import settings
from core.infrastructure.db import SessionLocal
from core.infrastructure.llm import OpenAILLM
from core.infrastructure.vector import PgVectorStore, TEIEmbedder
from rag import build_pipeline
from rag.config_store import load_config
from rag.eval import DEFAULT_GOLDEN, run_golden_set


def _pipeline_factory():
    cfg = asyncio.run(load_config(SessionLocal))
    return build_pipeline(
        embedder=TEIEmbedder(),
        vector_store=PgVectorStore(SessionLocal),
        session_factory=SessionLocal,
        llm=OpenAILLM(),
        settings=settings,
        config=cfg,
    )


async def main(args) -> int:
    report = await run_golden_set(_pipeline_factory, args.golden, args.top_k)

    print(f"\nGolden-set regression (top-{report.top_k}):")
    print(f"{'case':<8} {'recall@k':<9} {'precision@k':<12} {'mrr':<6} query")
    print("-" * 60)
    for c in report.cases:
        print(
            f"{c.case_id:<8} {c.recall_at_k:<9} {c.precision_at_k:<12} {c.mrr:<6} {c.query[:40]}"
        )
    m = report.metrics
    print("-" * 60)
    print(
        f"n={m['n_cases']}  avg_recall={m['avg_recall']}  "
        f"avg_precision={m['avg_precision']}  avg_mrr={m['avg_mrr']}"
    )

    if args.out:
        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out = out_dir / f"report-{ts}.json"
        out.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the golden-set RAG regression.")
    parser.add_argument("--golden", default=str(DEFAULT_GOLDEN), help="golden set JSON path")
    parser.add_argument("--top-k", type=int, default=5, help="recall depth")
    parser.add_argument("--out", default="data/eval/results", help="report output dir ('' = skip)")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args)))
