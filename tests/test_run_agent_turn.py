"""Background agent turns (Phase L): run_agent_turn reuses the hardened kernel.

A scheduled / cron turn must behave exactly like an interactive one: same kernel singleton,
same session memory, same channel pinning — and it must defer session_finalize.
"""
from types import SimpleNamespace
from uuid import uuid4

from agent.engine.loop import AgentResult

from apps.worker import tasks


class _FakeSessionMemory:
    def __init__(self, *args):
        self.args = args
        self.history = [{"role": "user", "content": "prior turn"}]

    async def load_messages(self):
        return self.history

    async def close(self):
        pass


class _FakeKernel:
    def __init__(self):
        self.run_calls = []

    async def run(self, message, history, **kwargs):
        self.run_calls.append((message, history, kwargs))
        return AgentResult(
            messages=[{"role": "assistant", "content": "done"}],
            final_answer="done",
            usage={"tokens": 7},
            cost_usd=0.01,
        )


class _JobStore:
    def __init__(self):
        self.calls = []
        self.created = []

    async def create(self, type_, payload, user_id=None):
        jid = uuid4()
        self.created.append((type_, payload, user_id))
        self.calls.append(("create", jid, type_))
        return SimpleNamespace(id=jid)

    async def mark_running(self, job_id, error=None):
        self.calls.append(("running", error))

    async def mark_succeeded(self, job_id, result):
        self.calls.append(("succeeded", result))

    async def mark_failed(self, job_id, error):
        self.calls.append(("failed", error))


class _Redis:
    def __init__(self):
        self.enqueued = []

    async def enqueue_job(self, type_, job_id, payload):
        self.enqueued.append((type_, str(job_id), payload))


def _ctx():
    return {
        "job_store": _JobStore(),
        "redis": _Redis(),
        "session_factory": object(),
        "embedder": object(),
        "llm": object(),
        "job_try": 1,
    }


async def test_run_agent_turn_runs_kernel_and_defers_finalize(monkeypatch):
    user_id, session_id = uuid4(), uuid4()
    kernel = _FakeKernel()
    monkeypatch.setattr(tasks, "get_agent_kernel", lambda: kernel)
    monkeypatch.setattr(tasks, "SessionMemoryStore", _FakeSessionMemory)
    ctx = _ctx()

    result = await tasks.run_agent_turn(
        ctx,
        str(uuid4()),
        {
            "user_id": str(user_id),
            "session_id": str(session_id),
            "message": "summarize today",
        },
    )

    assert result["final_answer"] == "done"
    assert result["steps"] == 1
    assert result["usage"] == {"tokens": 7}
    assert result["cost_usd"] == 0.01

    # The kernel got the message + loaded history. No channel kwargs: the LLM channel
    # is pinned by _run into the request context at job start; the payload/args never
    # carry model/base_url/api_key (plaintext-key + SSRF doctrine).
    message, history, kwargs = kernel.run_calls[0]
    assert message == "summarize today"
    assert history == [{"role": "user", "content": "prior turn"}]
    assert "base_url" not in kwargs and "api_key" not in kwargs
    assert isinstance(kwargs["session_memory"], _FakeSessionMemory)

    # Session memory was wired to the requested user + session.
    sm_args = kwargs["session_memory"].args
    assert sm_args[3] == session_id and sm_args[4] == user_id

    # session_finalize deferred like the interactive path — and it carries the owner so
    # the follow-up job's summarizer is gateway-pinned too.
    assert len(ctx["redis"].enqueued) == 1
    enq_type, _enq_job_id, enq_payload = ctx["redis"].enqueued[0]
    assert enq_type == "session_finalize"
    assert enq_payload == {"session_id": str(session_id)}
    finalize_creations = [c for c in ctx["job_store"].created if c[0] == "session_finalize"]
    assert finalize_creations and finalize_creations[0][2] == user_id
    # Honest job status: running then succeeded.
    assert ("running", None) in ctx["job_store"].calls
    assert ctx["job_store"].calls[-1][0] == "succeeded"


async def test_run_agent_turn_ignores_payload_channel_fields(monkeypatch):
    """Legacy/attacker payload carrying base_url/api_key: the fields are DEAD — the
    kernel call must not receive them (no SSRF / no plaintext-key bypass)."""
    user_id, session_id = uuid4(), uuid4()
    kernel = _FakeKernel()
    monkeypatch.setattr(tasks, "get_agent_kernel", lambda: kernel)
    monkeypatch.setattr(tasks, "SessionMemoryStore", _FakeSessionMemory)
    ctx = _ctx()

    await tasks.run_agent_turn(
        ctx,
        str(uuid4()),
        {
            "user_id": str(user_id),
            "session_id": str(session_id),
            "message": "hi",
            "model": "evil",
            "base_url": "http://attacker.example",
            "api_key": "sk-leak",
        },
    )

    kwargs = kernel.run_calls[0][2]
    assert kwargs.get("model") is None
    assert "base_url" not in kwargs and "api_key" not in kwargs
