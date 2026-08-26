"""FastAPI app: expose core use cases as REST/SSE.

Start: uvicorn api.main:app --reload
"""
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from agent.security.approvals import configure_approval_broker
from api.routers.admin import router as admin_router
from api.routers.auth import AVATAR_DIR
from api.routers.auth import router as auth_router
from api.routers.chat import router as chat_router
from api.routers.config import _bootstrap_config
from api.routers.config import router as config_router
from api.routers.drive import files as drive_files_router
from api.routers.drive import folders as drive_folders_router
from api.routers.drive import trash as drive_trash_router
from api.routers.drive import users as drive_users_router
from api.routers.drive import workspaces as drive_workspaces_router
from api.routers.jobs import router as jobs_router
from api.routers.rag_admin import router as rag_admin_router
from api.routers.sessions import router as sessions_router
from api.routers.vocab import router as vocab_router
from arq import create_pool
from arq.connections import RedisSettings
from core.application.drive_service import DriveError
from core.application.services import VocabError
from core.config import settings
from core.infrastructure.db import SessionLocal, init_db
from core.infrastructure.security import ensure_admin_user, ensure_default_admin
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from rag.query_cache import configure_query_cache

logger = logging.getLogger("uvicorn.error")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.bash_sandbox == "host":
        logger.warning(
            "bash_sandbox=\"host\": the agent bash tool runs directly on the host process. "
            "This is NOT a security boundary — use it for local dev only; the production "
            "default is \"docker\" (per-command container sandbox)."
        )
    await init_db()
    async with SessionLocal() as session:
        await ensure_default_admin(session)
        await ensure_admin_user(session)
        await _bootstrap_config(session)
    redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    app.state.redis = redis
    configure_approval_broker(redis)  # distributed approval wakeup across API nodes
    configure_query_cache(redis)      # RAG query cache (keyed by config + corpus version)
    yield
    await redis.aclose()


app = FastAPI(title="DeepDive API", version="0.1.0", lifespan=lifespan)


@app.exception_handler(VocabError)
async def _vocab_error_handler(request: Request, exc: VocabError):
    """Map vocabulary domain errors (403/404/etc.) to JSON responses."""
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})


@app.exception_handler(DriveError)
async def _drive_error_handler(request: Request, exc: DriveError):
    """Map cloud-drive domain errors (403/404/409/etc.) to JSON responses."""
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})


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

# User avatar uploads (self-service profile).
AVATAR_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/avatars", StaticFiles(directory=AVATAR_DIR.resolve()), name="avatars")

# Cloud drive: workspaces + file upload lifecycle / download / sharing / RAG status.
app.include_router(drive_workspaces_router)
app.include_router(drive_files_router)
app.include_router(drive_folders_router)
app.include_router(drive_trash_router)
app.include_router(drive_users_router)

# Functional groups (each router owns one domain; paths are unchanged from the monolith).
app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(config_router)
app.include_router(vocab_router)
app.include_router(chat_router)
app.include_router(sessions_router)
app.include_router(jobs_router)
app.include_router(rag_admin_router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
