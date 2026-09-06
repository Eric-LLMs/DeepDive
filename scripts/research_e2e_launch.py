"""Headless launcher for a fresh Research OS E2E run (new edition of a finished task).

BURNS REAL LLM TOKENS. Run it only when you actually want to benchmark the wholesale
EVIDENCE path (fetch/verify/read) against the historical baseline, which is recorded on
the "如何炒西红柿" task 2cbb0ca0 as run_seq=3 (Run e42eaf04, EVIDENCE window
23:12:46 -> 23:17:30, ~4.7 min, serial loop).

It mirrors exactly what ``apps/api/routers/chat.py`` does for the desktop "Run" control
on a finished task, minus the interactive turn-0 LLM call:

  1. ``begin_run(new_edition=True)`` — PUBLISH has no legal next stage, so a new edition
     resets the task: stage -> DISCOVER, gates -> NOT_RUN, evidence graph emptied,
     ``run_seq`` +1 (this edition lands in temp/vN + outputs/_vN).
  2. record the driver checkpoint as "turn 0 done" (the interactive turn's tail) — the
     driver CAS-checks this before admitting auto-turn 1.
  3. enqueue one RESEARCH_DRIVE job with ``turn_index: 1``. The worker (rebuilt image
     with the batch EVIDENCE code) then drives DISCOVER -> ... -> PUBLISH, re-enqueuing
     itself after every auto turn.

The worker resolves the owner's LLM channel from the DB on every spawn
(``resolve_channel_for_owner``); this script pre-flights that resolution and refuses to
enqueue when no channel exists, so it never leaves a doomed job that would 401-loop.

Usage (from the repo root, host venv):
    .venv/Scripts/python.exe scripts/research_e2e_launch.py [--yes]
or, to run against a different owner/task/session:
    .venv/Scripts/python.exe scripts/research_e2e_launch.py \
        --owner 61f6bb80-ca7c-4412-bed3-ced2853a45c6 --task <task-uuid>

Interactive runs ask for a y/N confirmation before spending anything; non-interactive
(stdin not a tty) runs require ``--yes``.

After the run reaches PUBLISH, gather the benchmark numbers from the task dir:
    run_events.json          — stage transitions (time the EVIDENCE -> DESIGN window)
    executions.json          — per-execution rows (LLM round-trip / tool-call counts)
    driver/cloud_assets      — the EVIDENCE batch fetch ledger + scraped full drafts
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from arq import create_pool
from arq.connections import RedisSettings
from core.application.drive_service import DriveService
from core.config import settings
from core.infrastructure.db import SessionLocal
from core.infrastructure.jobs import JobStore, TaskQueue

DEFAULT_OWNER = uuid.UUID("dd8297e5-7c3a-4eec-8930-5401af57ed61")  # tomato task owner
DEFAULT_TASK = "2cbb0ca0-bea0-4aa5-bbf3-a161fd62b09e"  # "如何炒西红柿" (baseline run_seq=3)


async def resolve_owner_channel(owner_id: uuid.UUID):
    """The owner's effective LLM channel from the DB, or ``None`` when none is usable."""
    from apps.api.routers._shared import resolve_channel_for_owner

    base_url, api_key, model, _business, _credential = await resolve_channel_for_owner(
        SessionLocal, owner_id
    )
    if not (base_url and api_key):
        return None
    return model, base_url, api_key


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--owner", type=uuid.UUID, default=DEFAULT_OWNER)
    parser.add_argument("--task", default=DEFAULT_TASK)
    parser.add_argument("--session", default=None, help="DB session id to drain events into")
    parser.add_argument("--yes", action="store_true", help="skip the y/N confirmation")
    args = parser.parse_args()

    # Imported here (after arg parsing) so ``--help`` never pulls the heavy plugin in.
    from plugins.research.plugin import ResearchService

    service = ResearchService(DriveService(SessionLocal), settings.research_scratch_dir)
    project = service.read_project(args.owner, args.task)
    if project is None:
        print(f"task not found: owner={args.owner} task={args.task}")
        return 2
    if project.get("stage") != "PUBLISH":
        print(
            f"task is at stage {project.get('stage')} — new_edition only takes effect on a "
            "PUBLISHED task; aborting to avoid a surprise mid-chain resume."
        )
        return 2

    channel = await resolve_owner_channel(args.owner)
    if channel is None:
        print("owner has no usable LLM channel in the DB — the run would 401-loop; aborting.")
        return 2
    model, base_url, api_key = channel

    session_id = args.session or project.get("session_id")
    print("── E2E run plan ──────────────────────────────────────")
    print(f"  owner       : {args.owner}")
    print(f"  task        : {args.task}  ({project.get('name')})")
    print(f"  profile     : {project.get('profile')}  mode={project.get('execution_mode')}")
    print(f"  prior run_seq: {project.get('run_seq')}  (new edition will use run_seq+1)")
    print(f"  session_id  : {session_id or '(none — events not drained to a chat)'}")
    print(f"  channel     : {model} @ {base_url}")
    print("  action      : begin_run(new_edition=True) → enqueue RESEARCH_DRIVE turn 1")
    print("  cost        : real LLM tokens for a full DISCOVER→PUBLISH auto-run")

    if not args.yes:
        if sys.stdin is None or not sys.stdin.isatty():
            print("stdin is not a tty — pass --yes to launch.")
            return 1
        try:
            answer = input("  Launch this run? [y/N] ").strip().lower()
        except EOFError:
            print("aborted (no input).")
            return 1
        if answer not in ("y", "yes"):
            print("aborted.")
            return 1

    # 1. Acquire the slot + reset to a fresh edition (sync; mirrors chat.py).
    run = service.begin_run(args.owner, args.task, session_id=session_id, new_edition=True)
    run_id = run["run_id"]

    # 2. Record the interactive turn-0 tail so the driver admits auto-turn 1 (chat.py).
    service.set_driver_checkpoint(
        args.owner,
        args.task,
        patch={
            "run_id": run_id,
            "turn_index": 0,
            "turn_attempt": 1,
            "turn_state": "done",
            "execution_id": f"{run_id}:0:1",
        },
    )

    # 3. Enqueue the first auto turn. PG job row first, then arq delivery.
    redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    try:
        queue = TaskQueue(redis, JobStore(SessionLocal))
        job_id = await queue.enqueue(
            "research_drive",
            {
                "user_id": str(args.owner),
                "task_id": args.task,
                "run_id": run_id,
                "session_id": session_id,
                "turn_index": 1,
                "model": model,
                "base_url": base_url,
                "api_key": api_key,
            },
            user_id=args.owner,
        )
    finally:
        await redis.close()

    print("── launched ─────────────────────────────────────────")
    print(f"  run_id      : {run_id}")
    print(f"  job_id      : {job_id}  (first RESEARCH_DRIVE auto-turn)")
    print(f"  new edition : temp/v{service.read_project(args.owner, args.task).get('run_seq')} "
          "+ outputs/_vN")
    print("  watch       : .venv/Scripts/python.exe logs/_watch_tomato_run.py  (adjust task id)")
    print("  when PUBLISHed, benchmark numbers live in run_events.json / executions.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
