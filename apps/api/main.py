"""FastAPI app: expose core use cases as REST/SSE.

Start: uvicorn api.main:app --reload
"""
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

from api.deps import get_agent, get_vocab_service, llm
from api.schemas import (
    BulkUpdateRequest,
    ChatRequest,
    DomainCreate,
    ExplainRequest,
    GenerateDefinitionRequest,
    ImageFetchRequest,
    ImportRequest,
    MatchCreate,
    SentenceCreate,
    SentenceImportRequest,
    SentenceUpdate,
    SyntaxAnalysisRequest,
    TermCreate,
    TermImportRequest,
    TermUpdate,
    TTSRequest,
)
from core.config import settings
from core.infrastructure.db import init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(title="DeepGloss API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # open during dev; tighten for production
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve cached TTS audio / images (paths produced by TTS and image scraping).
for _dir in (settings.audio_cache_path, settings.image_cache_path):
    _dir.mkdir(parents=True, exist_ok=True)
app.mount("/audio", StaticFiles(directory=Path(settings.audio_cache_path).resolve()), name="audio")
app.mount("/images", StaticFiles(directory=Path(settings.image_cache_path).resolve()), name="images")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


# ── Vocabulary domain ──
@app.post("/domains")
async def create_domain(body: DomainCreate, svc=Depends(get_vocab_service)):
    return await svc.add_domain(body.name)


@app.get("/domains")
async def list_domains(svc=Depends(get_vocab_service)):
    return await svc.list_domains()


@app.post("/terms")
async def create_term(body: TermCreate, svc=Depends(get_vocab_service)):
    return await svc.add_term(body.domain_id, body.word, body.definition)


@app.get("/domains/{domain_id}/terms")
async def list_terms(domain_id: UUID, svc=Depends(get_vocab_service)):
    return await svc.list_terms(domain_id)


@app.post("/terms/update")
async def update_term(body: TermUpdate, svc=Depends(get_vocab_service)):
    await svc.update_term(
        body.term_id,
        body.definition,
        body.audio_hash,
        body.star_level,
        body.image_paths,
        body.is_active,
    )
    return {"status": "ok"}


@app.post("/terms/bulk-update")
async def bulk_update_terms(body: BulkUpdateRequest, svc=Depends(get_vocab_service)):
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
    await svc.bulk_update_terms(updates)
    return {"status": "ok"}


@app.post("/terms/import")
async def import_terms(body: ImportRequest, svc=Depends(get_vocab_service)):
    return await svc.import_terms(body.domain_id, body.text)


@app.post("/terms/import-structured")
async def import_terms_structured(body: TermImportRequest, svc=Depends(get_vocab_service)):
    items = [(i.word, i.definition, i.frequency, i.star_level) for i in body.items]
    return await svc.import_terms_structured(body.domain_id, items)


@app.post("/sentences/import")
async def import_sentences(body: ImportRequest, svc=Depends(get_vocab_service)):
    return await svc.import_sentences(body.domain_id, body.text)


@app.post("/sentences/import-structured")
async def import_sentences_structured(body: SentenceImportRequest, svc=Depends(get_vocab_service)):
    return await svc.import_sentences_structured(body.domain_id, body.items)


@app.post("/image-fetch")
async def fetch_images(body: ImageFetchRequest, svc=Depends(get_vocab_service)):
    return {"image_paths": await svc.fetch_term_images(body.word, body.definition, body.context, body.regenerate)}


# ── Sentences ──
@app.post("/sentences")
async def create_sentence(body: SentenceCreate, svc=Depends(get_vocab_service)):
    return await svc.add_sentence(body.domain_id, body.content_en)


@app.post("/sentences/update")
async def update_sentence(body: SentenceUpdate, svc=Depends(get_vocab_service)):
    await svc.update_sentence(body.sentence_id, body.content_cn, body.audio_hash)
    return {"status": "ok"}


@app.get("/domains/{domain_id}/sentences")
async def list_sentences(domain_id: UUID, svc=Depends(get_vocab_service)):
    return await svc.list_sentences(domain_id)


@app.get("/domains/{domain_id}/sentences/search")
async def search_sentences(domain_id: UUID, q: str, svc=Depends(get_vocab_service)):
    return await svc.search_sentences(domain_id, q)


@app.post("/domains/{domain_id}/sentences/index")
async def index_sentences(domain_id: UUID, svc=Depends(get_vocab_service)):
    return await svc.index_sentences(domain_id)


@app.get("/domains/{domain_id}/sentences/semantic")
async def semantic_search(domain_id: UUID, q: str, svc=Depends(get_vocab_service)):
    return await svc.search_sentences_semantic(domain_id, q)


# ── Term ↔ sentence relations ──
@app.post("/matches")
async def link_term_to_sentence(body: MatchCreate, svc=Depends(get_vocab_service)):
    await svc.link_term_to_sentence(body.term_id, body.sentence_id, body.explanation)
    return {"status": "ok"}


@app.get("/terms/{term_id}/sentences")
async def list_sentences_for_term(term_id: UUID, svc=Depends(get_vocab_service)):
    return await svc.list_sentences_for_term(term_id)


# ── TTS ──
@app.post("/tts")
async def synthesize_audio(body: TTSRequest, svc=Depends(get_vocab_service)):
    path = await svc.synthesize_audio(body.text)
    if not path:
        raise HTTPException(status_code=500, detail="TTS synthesis failed")
    return {"url": f"/audio/{Path(path).name}"}


# ── AI capabilities ──
@app.post("/explain")
async def explain(body: ExplainRequest, svc=Depends(get_vocab_service)):
    return await svc.explain_term_in_context(body.term, body.context)


@app.post("/terms/definition")
async def generate_definition(body: GenerateDefinitionRequest, svc=Depends(get_vocab_service)):
    return {"definition": await svc.generate_definition(body.term)}


@app.post("/sentences/analyze")
async def analyze_syntax(body: SyntaxAnalysisRequest, svc=Depends(get_vocab_service)):
    return {"analysis": await svc.analyze_syntax(body.sentence)}


# ── Chat (Agent) ──
@app.post("/chat")
async def chat(body: ChatRequest):
    agent = get_agent()
    messages = await agent.run(body.message, body.history)
    answer = next(
        (m["content"] for m in reversed(messages) if m["role"] == "assistant" and m.get("content")),
        "",
    )
    return {"answer": answer, "messages": messages}


@app.post("/chat/stream")
async def chat_stream(body: ChatRequest):
    """SSE streaming (demonstrates real streaming output; the Agent streaming variant will follow up with complete_stream)."""

    async def gen():
        async for token in llm.complete_stream(body.message):
            yield {"data": token}

    return EventSourceResponse(gen())
