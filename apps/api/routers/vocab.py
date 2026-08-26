"""Vocabulary domain routes: domains / terms / sentences / matches / learning articles.

Tenant isolation: logged-in users see public + their own; guests see public only.
"""
from __future__ import annotations

from uuid import UUID

from api.auth import AuthUser, require_user, require_user_optional
from api.deps import _batch_embedder, get_task_queue, get_vocab_service, llm
from api.schemas import (
    ArticleCreateRequest,
    BulkUpdateRequest,
    DomainCreate,
    GenerateDefinitionRequest,
    ImportRequest,
    LearningImportRequest,
    MatchCreate,
    SentenceCreate,
    SentenceImportRequest,
    SentenceUpdate,
    SyntaxAnalysisRequest,
    TermCreate,
    TermImportRequest,
    TermUpdate,
)
from core.infrastructure.db import SessionLocal
from core.infrastructure.drive_repositories import SqlChunkRepository
from core.infrastructure.ingest import build_chunks, write_query_repo_chunks
from core.infrastructure.jobs import (
    ANALYZE_SYNTAX,
    GENERATE_DEFINITION,
    INDEX_SENTENCES,
    LEARNING_IMPORT,
    TaskQueue,
)
from core.infrastructure.repositories import SqlArticleRepository
from fastapi import APIRouter, Depends, HTTPException

router = APIRouter(tags=["vocab"])


def _uid(user: AuthUser | None) -> UUID | None:
    return user.user_id if user is not None else None


@router.post("/domains")
async def create_domain(
    body: DomainCreate,
    user: AuthUser | None = Depends(require_user_optional),
    svc=Depends(get_vocab_service),
):
    return await svc.add_domain(body.name, _uid(user))


@router.get("/domains")
async def list_domains(
    user: AuthUser | None = Depends(require_user_optional),
    svc=Depends(get_vocab_service),
):
    return await svc.list_domains(_uid(user))


@router.post("/domains/{domain_id}/clone")
async def clone_domain(
    domain_id: UUID,
    user: AuthUser = Depends(require_user),
    svc=Depends(get_vocab_service),
):
    return await svc.clone_domain(user.user_id, domain_id)


@router.post("/terms")
async def create_term(
    body: TermCreate,
    user: AuthUser | None = Depends(require_user_optional),
    svc=Depends(get_vocab_service),
):
    return await svc.add_term(body.domain_id, body.word, body.definition, _uid(user))


@router.get("/domains/{domain_id}/terms")
async def list_terms(
    domain_id: UUID,
    user: AuthUser | None = Depends(require_user_optional),
    svc=Depends(get_vocab_service),
):
    return await svc.list_terms(domain_id, user_id=_uid(user))


@router.post("/terms/update")
async def update_term(
    body: TermUpdate,
    user: AuthUser | None = Depends(require_user_optional),
    svc=Depends(get_vocab_service),
):
    await svc.update_term(
        body.term_id,
        body.definition,
        body.audio_hash,
        body.star_level,
        body.image_paths,
        body.is_active,
        user_id=_uid(user),
    )
    return {"status": "ok"}


@router.post("/terms/bulk-update")
async def bulk_update_terms(
    body: BulkUpdateRequest,
    user: AuthUser | None = Depends(require_user_optional),
    svc=Depends(get_vocab_service),
):
    updates = [
        {
            "id": u.term_id,
            "word": u.word,
            "definition": u.definition,
            "star_level": u.star_level,
            "is_active": u.is_active,
            "frequency": u.frequency,
        }
        for u in body.updates
    ]
    await svc.bulk_update_terms(updates, _uid(user))
    return {"status": "ok"}


@router.post("/terms/import")
async def import_terms(
    body: ImportRequest,
    user: AuthUser | None = Depends(require_user_optional),
    svc=Depends(get_vocab_service),
):
    return await svc.import_terms(body.domain_id, body.text, _uid(user))


@router.post("/terms/import-structured")
async def import_terms_structured(
    body: TermImportRequest,
    user: AuthUser | None = Depends(require_user_optional),
    svc=Depends(get_vocab_service),
):
    items = [(i.word, i.definition, i.frequency, i.star_level) for i in body.items]
    return await svc.import_terms_structured(body.domain_id, items, _uid(user))


@router.post("/sentences/import")
async def import_sentences(
    body: ImportRequest,
    user: AuthUser | None = Depends(require_user_optional),
    svc=Depends(get_vocab_service),
):
    return await svc.import_sentences(body.domain_id, body.text, _uid(user))


@router.post("/sentences/import-structured")
async def import_sentences_structured(
    body: SentenceImportRequest,
    user: AuthUser | None = Depends(require_user_optional),
    svc=Depends(get_vocab_service),
):
    return await svc.import_sentences_structured(body.domain_id, body.items, _uid(user))


@router.post("/learning/import")
async def learning_import_to_repo(
    body: LearningImportRequest,
    user: AuthUser = Depends(require_user),
    queue: TaskQueue = Depends(get_task_queue),
):
    """Push selected Learning-Platform sentences / articles into the query repository.

    The worker chunks each id under the runtime pipeline config and writes
    ``source_type='learning'`` + ``source_id=<id>`` chunks; re-importing is idempotent.
    """
    if body.kind not in ("sentence", "article"):
        raise HTTPException(status_code=400, detail="kind must be 'sentence' or 'article'")
    if not body.ids:
        raise HTTPException(status_code=400, detail="no ids given")
    job_id = await queue.enqueue(
        LEARNING_IMPORT,
        {"user_id": str(user.user_id), "kind": body.kind, "ids": body.ids},
        user_id=user.user_id,
    )
    return {"job_id": str(job_id)}


@router.post("/learning/articles", status_code=201)
async def create_article(
    body: ArticleCreateRequest,
    user: AuthUser = Depends(require_user),
):
    """Create a Learning-Platform article; import into the query repo is a separate call."""
    async with SessionLocal() as session:
        article = await SqlArticleRepository(session).add(
            user.user_id,
            body.title,
            body.content,
            UUID(body.domain_id) if body.domain_id else None,
        )
    return {"id": str(article.id), "title": article.title, "created_at": article.created_at}


@router.get("/learning/articles")
async def list_articles(
    user: AuthUser = Depends(require_user),
):
    """List the caller's articles, newest first."""
    async with SessionLocal() as session:
        articles = await SqlArticleRepository(session).list_by_user(user.user_id)
    return {
        "items": [
            {
                "id": str(a.id),
                "title": a.title,
                "content": a.content,
                "domain_id": str(a.domain_id) if a.domain_id else None,
                "created_at": a.created_at,
            }
            for a in articles
        ]
    }


@router.delete("/learning/articles/{article_id}")
async def delete_article(
    article_id: UUID,
    user: AuthUser = Depends(require_user),
):
    """Delete an article (owner only) and drop its query-repo chunks."""
    async with SessionLocal() as session:
        repo = SqlArticleRepository(session)
        article = await repo.get(article_id)
        if article is None or article.user_id != user.user_id:
            raise HTTPException(status_code=404, detail="article not found")
        await repo.delete(article_id)
    await SqlChunkRepository(SessionLocal).delete_by_source("learning", [str(article_id)])
    return {"status": "ok"}


@router.post("/learning/articles/{article_id}/import")
async def import_article_to_repo(
    article_id: UUID,
    user: AuthUser = Depends(require_user),
):
    """Chunk + embed a single article into the query repository (source_type='learning')."""
    async with SessionLocal() as session:
        article = await SqlArticleRepository(session).get(article_id)
    if article is None or article.user_id != user.user_id:
        raise HTTPException(status_code=404, detail="article not found")

    from rag.config_store import load_config  # lazy: rag is a sibling package

    cfg = await load_config(SessionLocal)
    chunks = await build_chunks(article.content, cfg, doc_title=article.title, llm=llm)
    for c in chunks:
        c.meta = {**c.meta, "title": article.title, "kind": "article"}
    chunks_repo = SqlChunkRepository(SessionLocal)
    await chunks_repo.delete_by_source("learning", [str(article_id)])
    res = await write_query_repo_chunks(
        SessionLocal,
        _batch_embedder(),
        chunks=chunks,
        user_id=user.user_id,
        source_type="learning",
        source_id=str(article_id),
    )
    return {"chunks": res["chunks"]}


@router.post("/sentences")
async def create_sentence(
    body: SentenceCreate,
    user: AuthUser | None = Depends(require_user_optional),
    svc=Depends(get_vocab_service),
):
    return await svc.add_sentence(body.domain_id, body.content_en, _uid(user))


@router.post("/sentences/update")
async def update_sentence(
    body: SentenceUpdate,
    user: AuthUser | None = Depends(require_user_optional),
    svc=Depends(get_vocab_service),
):
    await svc.update_sentence(body.sentence_id, body.content_cn, body.audio_hash, _uid(user))
    return {"status": "ok"}


@router.get("/domains/{domain_id}/sentences")
async def list_sentences(
    domain_id: UUID,
    user: AuthUser | None = Depends(require_user_optional),
    svc=Depends(get_vocab_service),
):
    return await svc.list_sentences(domain_id, _uid(user))


@router.get("/domains/{domain_id}/sentences/search")
async def search_sentences(
    domain_id: UUID,
    q: str,
    user: AuthUser | None = Depends(require_user_optional),
    svc=Depends(get_vocab_service),
):
    return await svc.search_sentences(domain_id, q, _uid(user))


@router.post("/domains/{domain_id}/sentences/index")
async def index_sentences(
    domain_id: UUID,
    user: AuthUser | None = Depends(require_user_optional),
    svc=Depends(get_vocab_service),
    queue: TaskQueue = Depends(get_task_queue),
):
    await svc.ensure_domain_access(_uid(user), domain_id)  # guard before enqueuing the job
    job_id = await queue.enqueue(INDEX_SENTENCES, {"domain_id": str(domain_id)})
    return {"job_id": str(job_id)}


@router.get("/domains/{domain_id}/sentences/semantic")
async def semantic_search(
    domain_id: UUID,
    q: str,
    user: AuthUser | None = Depends(require_user_optional),
    svc=Depends(get_vocab_service),
):
    return await svc.search_sentences_semantic(domain_id, q, user_id=_uid(user))


@router.post("/matches")
async def link_term_to_sentence(
    body: MatchCreate,
    user: AuthUser | None = Depends(require_user_optional),
    svc=Depends(get_vocab_service),
):
    await svc.link_term_to_sentence(body.term_id, body.sentence_id, body.explanation, _uid(user))
    return {"status": "ok"}


@router.get("/terms/{term_id}/sentences")
async def list_sentences_for_term(
    term_id: UUID,
    user: AuthUser | None = Depends(require_user_optional),
    svc=Depends(get_vocab_service),
):
    return await svc.list_sentences_for_term(term_id, _uid(user))


@router.post("/terms/definition")
async def generate_definition(body: GenerateDefinitionRequest, queue: TaskQueue = Depends(get_task_queue)):
    job_id = await queue.enqueue(GENERATE_DEFINITION, {"term": body.term})
    return {"job_id": str(job_id)}


@router.post("/sentences/analyze")
async def analyze_syntax(body: SyntaxAnalysisRequest, queue: TaskQueue = Depends(get_task_queue)):
    job_id = await queue.enqueue(ANALYZE_SYNTAX, {"sentence": body.sentence})
    return {"job_id": str(job_id)}
