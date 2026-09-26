"""Live-table query corpus plane (migration 0014, final ruling 2026-09-26).

The ONLY way a sentence reaches the runtime corpus: straight into
``capability_standard_queries`` / ``capability_similar_queries`` /
``capability_negatives`` — no Draft, no Publish, no Projection.

Atomicity ruling enforced here: query text + embedding are ONE unit. The
embedding is generated FIRST; an embedding failure aborts the whole
modification and the old data keeps serving. No active query may exist
without an embedding (enable is refused while the vector is NULL).

Language is materialized on write with the frozen derivation rule
(Han char U+4E00-U+9FFF -> 'zh', else 'en'); a Similar's language must equal
its Standard's, so a derivation mismatch is rejected, never auto-fixed.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import func, select, update

from core.infrastructure.db import (
    CapabilityModel,
    CapabilityNegativeModel,
    CapabilitySimilarQueryModel,
    CapabilityStandardQueryModel,
    SessionLocal,
)

from .entry import derive_language
from .store import (
    RegistryConflictError,
    RegistryError,
    RegistryNotFoundError,
    invalidate_cache,
)

_TABLES = {
    "standard": CapabilityStandardQueryModel,
    "similar": CapabilitySimilarQueryModel,
    "negative": CapabilityNegativeModel,
}


def _factory(session_factory: Any = None):
    return session_factory or SessionLocal


async def _embed_one(embedder, text: str) -> list[float]:
    """Embed BEFORE any write: a failure raises here and the modification
    never starts (text+embedding atomicity)."""
    if embedder is None:
        raise RegistryError("embedder unavailable: query writes are refused")
    vectors = await embedder.embed([text])
    if not isinstance(vectors, list) or not vectors or not vectors[0]:
        raise RegistryError("embedder returned no vector; write aborted")
    return list(vectors[0])


async def list_queries(capability_id: str, *,
                       session_factory: Any = None) -> dict:
    """The editable query view for one capability (admin editor feed)."""
    factory = _factory(session_factory)
    async with factory() as session:
        if (await session.execute(
            select(CapabilityModel.capability_id).where(
                CapabilityModel.capability_id == capability_id)
        )).scalar_one_or_none() is None:
            raise RegistryNotFoundError(f"no capability {capability_id!r}")
        std = (await session.execute(
            select(CapabilityStandardQueryModel)
            .where(CapabilityStandardQueryModel.capability_id == capability_id)
            .order_by(CapabilityStandardQueryModel.position)
        )).scalars().all()
        std_ids = [r.id for r in std]
        sim = (await session.execute(
            select(CapabilitySimilarQueryModel)
            .where(CapabilitySimilarQueryModel.standard_query_id.in_(std_ids))
            .order_by(CapabilitySimilarQueryModel.position)
        )).scalars().all() if std_ids else []
        neg = (await session.execute(
            select(CapabilityNegativeModel)
            .where(CapabilityNegativeModel.capability_id == capability_id)
            .order_by(CapabilityNegativeModel.position)
        )).scalars().all()

    def _row(r: Any, embedded: bool | None = None) -> dict:
        return {
            "id": str(r.id), "query": r.query, "language": r.language,
            "enabled": bool(r.enabled), "position": int(r.position or 0),
            "has_embedding": (bool(r.embedding is not None) if embedded is None
                              else embedded),
        }
    return {
        "capability_id": capability_id,
        "standard": [_row(r) for r in std],
        "similar": [dict(_row(r), standard_query_id=str(r.standard_query_id))
                    for r in sim],
        "negative": [
            {"id": str(r.id), "query": r.query, "language": r.language,
             "enabled": bool(r.enabled), "position": int(r.position or 0)}
            for r in neg
        ],
    }


# ── Writes (embed-then-write, atomic) ────────────────────────────────────────────

async def add_standard_query(capability_id: str, query: str, *, embedder,
                             position: int = 0,
                             session_factory: Any = None) -> dict:
    query = str(query or "").strip()
    if not query:
        raise ValueError("query must not be blank")
    language = derive_language(query)
    factory = _factory(session_factory)
    async with factory() as session:
        if (await session.execute(
            select(CapabilityModel.capability_id).where(
                CapabilityModel.capability_id == capability_id)
        )).scalar_one_or_none() is None:
            raise RegistryNotFoundError(f"no capability {capability_id!r}")
        taken = (await session.execute(
            select(CapabilityStandardQueryModel.language).where(
                CapabilityStandardQueryModel.capability_id == capability_id,
                CapabilityStandardQueryModel.language == language,
            )
        )).scalar_one_or_none()
        if taken is not None:
            raise RegistryConflictError(
                f"{capability_id} already has a '{language}' standard query; "
                "edit it instead (one per language)")
    vector = await _embed_one(embedder, query)
    row = CapabilityStandardQueryModel(
        capability_id=capability_id, query=query, language=language,
        embedding=vector, position=position)
    async with factory() as session:
        session.add(row)
        try:
            await session.flush()
            new_id = str(row.id)  # resolved by the INSERT's post-fetch, before expire
            await session.commit()
        except Exception:
            await session.rollback()
            raise
    invalidate_cache()
    return {"id": new_id, "query": query, "language": language}


async def add_similar_query(standard_query_id: str, query: str, *, embedder,
                            position: int = 0,
                            session_factory: Any = None) -> dict:
    query = str(query or "").strip()
    if not query:
        raise ValueError("query must not be blank")
    language = derive_language(query)
    factory = _factory(session_factory)
    std_pk = _uuid(standard_query_id)
    async with factory() as session:
        std = await session.get(CapabilityStandardQueryModel, std_pk)
        if std is None:
            raise RegistryNotFoundError(f"no standard query {standard_query_id!r}")
        if std.language != language:
            raise ValueError(
                f"similar language {language!r} disagrees with its standard "
                f"query language {std.language!r}")
    vector = await _embed_one(embedder, query)
    row = CapabilitySimilarQueryModel(
        standard_query_id=std_pk, query=query, language=language,
        embedding=vector, position=position)
    async with factory() as session:
        session.add(row)
        try:
            await session.flush()
            new_id = str(row.id)
            await session.commit()
        except Exception:
            await session.rollback()
            raise
    invalidate_cache()
    return {"id": new_id, "query": query, "language": language}


async def add_negative_query(capability_id: str, query: str, *,
                             position: int = 0,
                             session_factory: Any = None) -> dict:
    """Negatives carry NO embedding by design: card boundary context only."""
    query = str(query or "").strip()
    if not query:
        raise ValueError("query must not be blank")
    language = derive_language(query)
    factory = _factory(session_factory)
    row = CapabilityNegativeModel(
        capability_id=capability_id, query=query, language=language,
        position=position)
    async with factory() as session:
        if (await session.execute(
            select(CapabilityModel.capability_id).where(
                CapabilityModel.capability_id == capability_id)
        )).scalar_one_or_none() is None:
            await session.rollback()
            raise RegistryNotFoundError(f"no capability {capability_id!r}")
        session.add(row)
        try:
            await session.flush()
            new_id = str(row.id)
            await session.commit()
        except Exception:
            await session.rollback()
            raise
    invalidate_cache()
    return {"id": new_id, "query": query, "language": language}


async def update_query_text(table: str, query_id: str, new_query: str, *,
                            embedder, session_factory: Any = None) -> dict:
    """Text + embedding rewrite as ONE unit: the new vector is produced first;
    only then does the row change. Language is re-derived on text change."""
    if table not in _TABLES or table == "negative":
        raise ValueError(f"table must be 'standard' or 'similar', got {table!r}")
    new_query = str(new_query or "").strip()
    if not new_query:
        raise ValueError("query must not be blank")
    factory = _factory(session_factory)
    pk = _uuid(query_id)
    language = derive_language(new_query)
    model = _TABLES[table]
    async with factory() as session:
        row = await session.get(model, pk)
        if row is None:
            raise RegistryNotFoundError(f"no {table} query {query_id!r}")
        if table == "similar":
            std = await session.get(CapabilityStandardQueryModel,
                                    row.standard_query_id)
            if std is None or std.language != language:
                raise ValueError(
                    f"new text derives {language!r}, which disagrees with the "
                    "standard query language")
        else:
            clash = (await session.execute(
                select(CapabilityStandardQueryModel.id).where(
                    CapabilityStandardQueryModel.capability_id == row.capability_id,
                    CapabilityStandardQueryModel.language == language,
                    CapabilityStandardQueryModel.id != pk,
                )
            )).scalar_one_or_none()
            if clash is not None:
                raise RegistryConflictError(
                    f"another standard query already holds language {language!r}")
    vector = await _embed_one(embedder, new_query)
    async with factory() as session:
        res = await session.execute(
            update(model).where(model.id == pk)
            .values(query=new_query, language=language, embedding=vector,
                    updated_at=func.now())
        )
        if res.rowcount == 0:
            await session.rollback()
            raise RegistryNotFoundError(f"no {table} query {query_id!r}")
        await session.commit()
    invalidate_cache()
    return {"id": query_id, "query": new_query, "language": language}


async def set_query_enabled(table: str, query_id: str, enabled: bool, *,
                            session_factory: Any = None) -> dict:
    """Soft disable/enable. Enabling is REFUSED while the embedding is NULL
    (no active query without a vector). Disabling a Standard that still has
    enabled Similar rows is refused too — the Similar would lose its only
    Capability relation."""
    if table not in _TABLES:
        raise ValueError(f"unknown table {table!r}")
    factory = _factory(session_factory)
    pk = _uuid(query_id)
    model = _TABLES[table]
    async with factory() as session:
        row = await session.get(model, pk)
        if row is None:
            raise RegistryNotFoundError(f"no {table} query {query_id!r}")
        if enabled and table != "negative" and row.embedding is None:
            raise RegistryConflictError(
                f"{table} query {query_id!r} has no embedding yet; "
                "run the backfill before enabling")
        if (not enabled and table == "standard"
                and (await session.execute(
                    select(CapabilitySimilarQueryModel.id).where(
                        CapabilitySimilarQueryModel.standard_query_id == pk,
                        CapabilitySimilarQueryModel.enabled.is_(True),
                    )
                )).scalar_one_or_none() is not None):
            raise RegistryConflictError(
                f"standard query {query_id!r} still has enabled similar rows; "
                "disable those first")
        row.enabled = bool(enabled)
        row.updated_at = func.now()
        await session.commit()
    invalidate_cache()
    return {"id": query_id, "enabled": bool(enabled)}


async def delete_query(table: str, query_id: str, *,
                       cascade_similar: bool = False,
                       session_factory: Any = None) -> dict:
    """Hard delete of a corpus row (the capability itself keeps the no-hard-
    delete lifecycle doctrine; sentences are editable assets, not history).
    A Standard with Similar children is refused unless ``cascade_similar``."""
    if table not in _TABLES:
        raise ValueError(f"unknown table {table!r}")
    factory = _factory(session_factory)
    pk = _uuid(query_id)
    model = _TABLES[table]
    async with factory() as session:
        row = await session.get(model, pk)
        if row is None:
            raise RegistryNotFoundError(f"no {table} query {query_id!r}")
        if table == "standard":
            kids = (await session.execute(
                select(CapabilitySimilarQueryModel)
                .where(CapabilitySimilarQueryModel.standard_query_id == pk)
            )).scalars().all()
            if kids and not cascade_similar:
                raise RegistryConflictError(
                    f"standard query {query_id!r} has {len(kids)} similar rows; "
                    "pass cascade_similar or delete them first")
            for k in kids:
                await session.delete(k)
        await session.delete(row)
        await session.commit()
    invalidate_cache()
    return {"deleted": query_id, "table": table}


# ── Bulk helpers (backfill + restore) ────────────────────────────────────────────

async def pending_embeddings(*, session_factory: Any = None) -> list[dict]:
    """Every Standard/Similar row still missing its vector: the backfill
    script's work list (fields: table, id, query)."""
    factory = _factory(session_factory)
    out: list[dict] = []
    async with factory() as session:
        for table, model in (("standard", CapabilityStandardQueryModel),
                             ("similar", CapabilitySimilarQueryModel)):
            rows = (await session.execute(
                select(model.id, model.query).where(model.embedding.is_(None))
            )).all()
            out.extend({"table": table, "id": str(r[0]), "query": r[1]}
                       for r in rows)
    return out


async def store_embedding(table: str, query_id: str, vector: Sequence[float],
                          *, session_factory: Any = None) -> None:
    """One backfill write (script-side atomicity is per row: text already
    exists; filling a vector never changes routing content)."""
    if table not in ("standard", "similar"):
        raise ValueError(f"unknown table {table!r}")
    model = _TABLES[table]
    async with _factory(session_factory)() as session:
        res = await session.execute(
            update(model).where(model.id == _uuid(query_id))
            .values(embedding=list(vector), updated_at=func.now())
        )
        if res.rowcount == 0:
            await session.rollback()
            raise RegistryNotFoundError(f"no {table} query {query_id!r}")
        await session.commit()
    invalidate_cache()


# ── helpers ──────────────────────────────────────────────────────────────────────

def _uuid(s: str):
    from uuid import UUID

    try:
        return UUID(str(s))
    except (ValueError, TypeError) as exc:
        raise ValueError(f"not a uuid: {s!r}") from exc
