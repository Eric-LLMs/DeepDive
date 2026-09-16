"""Kick slides generation from the Agent Harness survey PDF on My Drive.

Sources: drive asset c0eec74a-de73-4c29-a0f7-42ec72702750
Output folder on drive: tools_test/
Polls the job to completion and dumps the result (incl. deck_stats) to JSON.
BURNS REAL LLM TOKENS.
"""
import asyncio
import json
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/packages")

from arq import create_pool
from arq.connections import RedisSettings
from core.config import settings
from core.infrastructure.db import SessionLocal
from core.infrastructure.jobs import JobStore, TaskQueue, TOOLKIT_GENERATE

OWNER = uuid.UUID("dd8297e5-7c3a-4eec-8930-5401af57ed61")
FILE_IDS = ["c0eec74a-de73-4c29-a0f7-42ec72702750"]
FOLDER_PATH = "tools_test"
OUT = Path("/tmp/_kick_tools_test_slides_result.json")
TIMEOUT_S = 2700


async def main() -> int:
    redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    store = JobStore(SessionLocal)
    try:
        queue = TaskQueue(redis, store)
        job_id = await queue.enqueue(
            TOOLKIT_GENERATE,
            {"tool": "slides", "file_ids": FILE_IDS, "folder_path": FOLDER_PATH,
             "name": "Agent Harness Engineering A Survey",
             "prompt": "幻灯片正文用中文,专业术语保留英文原文",
             "count": 10,
             "audience": "工程团队内部技术评审",
             "goal": "讲清 Agent Harness Engineering 的定义、核心组件、研究版图与工程取舍",
             "language": "zh", "format_mode": "detailed"},
            user_id=OWNER,
        )
        jid = str(job_id)
        OUT.write_text(json.dumps({"job_id": jid, "status": "running", "start_ts": time.time()}))
        print(f"enqueued job {jid}", flush=True)
        t0 = time.time()
        while time.time() - t0 < TIMEOUT_S:
            row = await store.get(job_id if isinstance(job_id, uuid.UUID) else uuid.UUID(jid))
            status = getattr(row, "status", None)
            if status in ("succeeded", "failed"):
                result = getattr(row, "result", None)
                error = getattr(row, "error", None)
                print(f"status={status} in {time.time() - t0:.0f}s", flush=True)
                OUT.write_text(json.dumps({
                    "job_id": jid, "status": status, "elapsed_s": time.time() - t0,
                    "result": result, "error": error,
                }, default=str, indent=2))
                print("result:", json.dumps(result, default=str)[:4000])
                print("error:", error)
                return 0 if status == "succeeded" else 1
            await asyncio.sleep(10)
        print("TIMEOUT waiting for job")
        return 1
    finally:
        await redis.aclose()


sys.exit(asyncio.run(main()))
