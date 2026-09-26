"""Embedding backfill for the LIVE intent corpus (migration 0014 tooling).

Order of operations pinned by the final ruling: migrate -> language
materialized by the migration -> validate -> **this script** -> HNSW live.
The migration itself never calls the embedding service; vectors are produced
out of band, per row.

* default: fill every Standard/Similar row whose embedding is NULL;
* ``--rebuild``: re-embed EVERY row (profile change / model swap), the "full
  rebuild capability";
* on success the script records the embedding profile fingerprint in
  ``app_settings.embedding_profile`` — model/provider/dim. Recall rows whose
  vectors came from a different profile can be detected (and rebuilt) with it.

Usage:
    python scripts/embed_corpus.py            # backfill pending rows
    python scripts/embed_corpus.py --rebuild  # re-embed the whole corpus
"""
from __future__ import annotations

import argparse
import asyncio

from core.application.chat.intent_funnel.registry import queries
from core.application.chat.intent_funnel.registry.store import embedding_status
from core.config import settings
from core.infrastructure.db import SessionLocal
from core.infrastructure.security import set_setting
from core.infrastructure.vector import TEIEmbedder

_BATCH = 16  # texts per /embed call; TEI handles this size well for short queries


def _profile() -> dict:
    return {
        "model": settings.embedding_model,
        "provider": "tei",
        "base_url": settings.embedding_base_url,
        "dim": int(settings.embedding_dim),
    }


async def _rows(*, rebuild: bool) -> list[dict]:
    if rebuild:
        from core.infrastructure.db import (
            CapabilitySimilarQueryModel,
            CapabilityStandardQueryModel,
        )
        from sqlalchemy import select

        out: list[dict] = []
        async with SessionLocal() as session:
            for table, model in (("standard", CapabilityStandardQueryModel),
                                 ("similar", CapabilitySimilarQueryModel)):
                rows = (await session.execute(
                    select(model.id, model.query))).all()
                out.extend({"table": table, "id": str(r[0]), "query": r[1]}
                           for r in rows)
        return out
    return await queries.pending_embeddings(session_factory=SessionLocal)


async def main(*, rebuild: bool) -> None:
    rows = await _rows(rebuild=rebuild)
    if not rows:
        print("nothing to embed — corpus fully vectorized")
    else:
        embedder = TEIEmbedder(timeout=120.0)
        done = 0
        for i in range(0, len(rows), _BATCH):
            batch = rows[i:i + _BATCH]
            vectors = await embedder.embed([r["query"] for r in batch])
            if len(vectors) != len(batch):
                raise SystemExit(
                    f"embedder returned {len(vectors)} vectors for {len(batch)} "
                    "texts — aborting; already-written rows stay (they are "
                    "complete), rerun to continue")
            for row, vec in zip(batch, vectors, strict=True):
                if not vec:
                    raise SystemExit(f"embedder returned an empty vector for "
                                     f"{row['table']}/{row['id']} — aborting")
                await queries.store_embedding(row["table"], row["id"], vec,
                                              session_factory=SessionLocal)
                done += 1
            print(f"embedded {done}/{len(rows)}")
        await embedder._client.aclose()
    async with SessionLocal() as session:
        await set_setting(session, "embedding_profile", _profile())
    status = await embedding_status(session_factory=SessionLocal)
    print("embedding_profile:", _profile())
    print("corpus:", status)
    pending = status["standard"]["total"] - status["standard"]["embedded"] \
        + status["similar"]["total"] - status["similar"]["embedded"]
    if pending:
        raise SystemExit(f"STILL {pending} rows without vectors — rerun to continue")
    print("corpus fully embedded; HNSW indexes live on the tables")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rebuild", action="store_true",
                        help="re-embed every row (profile change / model swap)")
    args = parser.parse_args()
    asyncio.run(main(rebuild=args.rebuild))
