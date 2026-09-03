"""Research monitor: cheap, lossy wake-up events for a task's authoritative state.

The desktop status panel and working-directory tree are rendered from a *snapshot*
(``GET /research/tasks/{id}``). They should refresh as a run progresses, but polling the
full task status (with its drive listing) every second is wasteful and racy. Instead the
research layer publishes a tiny invalidation hint whenever a user-visible change commits:

    Redis pub/sub ``research:monitor:{task_id}`` -> ``{"project_revision", "kind", "ts"}``

``project_revision`` is the domain-authoritative monotonic version (bumped inside every
``atomic_update_project`` commit). The API's SSE monitor subscribes *before* sending its
snapshot and forwards only events newer than the snapshot, so the desktop can refetch the
authoritative detail exactly when something changed — never on a blind timer.

Semantics: **hint, not contract.** If the subscriber is away or Redis drops the message,
the client's own poll/reconnect path converges; the file state stays the source of truth.
"""
from __future__ import annotations

from core.infrastructure.redis_bus import publish as _bus_publish

# Redis channel prefix: ``research:monitor:{task_id}``.
MONITOR_CHANNEL_PREFIX = "research:monitor:"

# Tool actions that produce a user-visible change. After one of these succeeds the monitor
# bumps the task revision and publishes a wake-up, so the desktop refetches its snapshot.
# Actions kept out of the set are read-only (resume/get_state/query_lineage/diff/read/…).
MUTATING_ACTIONS: dict[str, set[str]] = {
    "research_project": {"archive"},
    "research_artifact": {"write_scratch", "create_version", "promote_to_drive"},
    "research_state": {"transition_stage"},
    "research_evidence": {
        "record_node",
        "link_edge",
        "mutate_node",
        "invalidate_downstream",
    },
    "research_gate": {"check", "explain_failure", "request_override", "resolve_override"},
    "research_run": set(),
}


def task_channel(task_id: str) -> str:
    return f"{MONITOR_CHANNEL_PREFIX}{task_id}"


async def publish_task_event(
    task_id: str, *, project_revision: int, kind: str, ts: str
) -> None:
    """Best-effort publish of one wake-up hint (never raises)."""
    await _bus_publish(
        task_channel(task_id),
        {"project_revision": project_revision, "kind": kind, "ts": ts},
    )
