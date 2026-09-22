"""Layer B — every certified phrasing of each ACTION tool dispatched through the REAL
funnel (charter category 4: "every tool phrasing").

Each row is a distinct surface form; each must (a) certify to the exact tool + args,
(b) execute the real registered tool body EXACTLY once, (c) surface the correct
permission decision (create_folder/add_term are WRITE-classified ⇒ ASK; the asset tool
is read-classified ⇒ no ASK), and (d) never enter the LLM/Agent loop. This proves the
fast path's side-effect count and permission behavior match the Agent for the full
phrase surface, not just one exemplar per tool.
"""
from __future__ import annotations

import pytest

from tests.p5_validation._p5_harness import (
    USER,
    FakeSeam,
    ScriptedPort,
    Spy,
    build_app,
    build_kernel,
    domains_named,
    sse,
)
from tests.p5_validation.test_p5_smoke import _gate

STEP = {"content": ["unused"], "tool_calls": None}

# (message, folder_name) — all must dispatch create_folder once, ASK surfaced.
CREATE_PHRASES = [
    ('create a folder named "alpha"', "alpha"),
    ('make a folder "beta"', "beta"),
    ('create the folder called "gamma"', "gamma"),
    ('make me a new directory titled "delta"', "delta"),
    ("创建文件夹“epsilon”", "epsilon"),
    ("新建一个叫「zeta」的文件夹", "zeta"),
    ("建立一个名为“eta”的目录", "eta"),
    ('create a folder named “theta”', "theta"),          # curly double quotes
    ('can you create a folder named "iota" thanks', "iota"),
]


@pytest.mark.parametrize("msg,name", CREATE_PHRASES,
                         ids=[f"cf{i}" for i in range(len(CREATE_PHRASES))])
async def test_create_folder_each_phrase(monkeypatch, msg, name):
    port = ScriptedPort(steps=[STEP]); spy = Spy()
    kernel, _, _, broker = build_kernel(monkeypatch, port, spy, broker_mode="allow")
    _gate(monkeypatch, action=True)
    app = build_app(monkeypatch, port, FakeSeam([]), kernel, broker)
    res = await sse(app, msg)
    assert spy.folders_created == [(str(USER), name)]       # exactly one, correct name
    assert res.approvals and res.approvals[0]["name"] == "create_folder"
    assert port.steps == 0 and port.single_shot == 0        # no LLM


ADD_PHRASES = [
    ('add "quantum" to my science vocab', "quantum"),
    ('add "entropy" to the physics vocabulary', "entropy"),
    ("把“熵”加入我的科学词汇库", "熵"),
    ("将“极限”添加到数学单词库", "极限"),
    ('add "lambda" to my math word list', "lambda"),
]


@pytest.mark.parametrize("msg,term", ADD_PHRASES,
                         ids=[f"at{i}" for i in range(len(ADD_PHRASES))])
async def test_add_term_each_phrase(monkeypatch, msg, term):
    port = ScriptedPort(steps=[STEP]); spy = Spy()
    kernel, _, _, broker = build_kernel(
        monkeypatch, port, spy, broker_mode="allow",
        domains=domains_named("Science", "科学", "Math", "physics", "数学"),
    )
    _gate(monkeypatch, action=True)
    app = build_app(monkeypatch, port, FakeSeam([]), kernel, broker)
    await sse(app, msg)
    assert len(spy.terms_added) == 1 and spy.terms_added[0][1] == term


# ── the ASK-vs-not asymmetry is recorded: create_folder (WRITE) ASKS, add_term does not ─
async def test_add_term_read_classified_no_ask(monkeypatch):
    port = ScriptedPort(steps=[STEP]); spy = Spy()
    kernel, _, _, broker = build_kernel(
        monkeypatch, port, spy, broker_mode="allow", domains=domains_named("Science"),
    )
    _gate(monkeypatch, action=True)
    app = build_app(monkeypatch, port, FakeSeam([]), kernel, broker)
    res = await sse(app, 'add "boson" to my science vocab')
    assert len(spy.terms_added) == 1
    assert res.approvals == []   # FINDING: add_term is READ-classified (no write-hint arg),
                                 # so it mutates WITHOUT approval, unlike create_folder.
