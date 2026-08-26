"""Bash sandbox: workspace-escape guard, host run behavior, factory, docker error path."""
import sys

import pytest
from agent.tools.bash_sandbox import (
    DockerBashSandbox,
    HostBashSandbox,
    assert_no_escape,
    get_bash_sandbox,
)
from core.config import settings


# ── workspace-escape guard (best-effort first line of defense) ──
def test_escape_guard_rejects_dotdot(tmp_path):
    with pytest.raises(ValueError, match="escapes workspace"):
        assert_no_escape(tmp_path, "cat ../secret")


def test_escape_guard_rejects_cd_outside_workspace(tmp_path):
    # ``cd ..`` walks up out of the workspace root.
    with pytest.raises(ValueError, match="escapes workspace"):
        assert_no_escape(tmp_path, "cd .. && ls")


def test_escape_guard_rejects_absolute_target(tmp_path):
    # ``/etc`` (or ``C:\\...`` on Windows) resolves outside the workspace root.
    with pytest.raises(ValueError, match="escapes workspace"):
        assert_no_escape(tmp_path, "cat /etc/passwd")


def test_escape_guard_allows_relative_commands(tmp_path):
    assert_no_escape(tmp_path, "ls -la .")
    assert_no_escape(tmp_path, "echo hello")


# ── host sandbox (dev-only fallback) ──
async def test_host_returns_trimmed_output(tmp_path):
    sb = HostBashSandbox(tmp_path)
    out = await sb.run("echo hello", 5)
    assert out == "hello"


async def test_host_raises_on_escape(tmp_path):
    sb = HostBashSandbox(tmp_path)
    with pytest.raises(ValueError, match="escapes workspace"):
        await sb.run("cat ../x", 5)


async def test_host_timeout_kills_process(tmp_path):
    sb = HostBashSandbox(tmp_path)
    with pytest.raises(TimeoutError, match="timed out"):
        await sb.run(f'"{sys.executable}" -c "import time; time.sleep(10)"', 1)


async def test_host_output_cap(tmp_path):
    sb = HostBashSandbox(tmp_path)
    sb._MAX_OUTPUT = 10
    out = await sb.run("echo 12345678901234567890", 5)
    assert out.endswith("(truncated)")


# ── docker sandbox (production path) ──
async def test_docker_missing_dependency_raises_clear_error(monkeypatch, tmp_path):
    sb = DockerBashSandbox(tmp_path)

    def _no_docker():
        raise RuntimeError("docker-py is not installed; install the 'docker' extra")

    monkeypatch.setattr(DockerBashSandbox, "_import_docker", staticmethod(_no_docker))
    with pytest.raises(RuntimeError, match="docker-py is not installed"):
        await sb.run("echo hi", 5)


def test_docker_decode_output():
    from agent.tools.bash_sandbox import _decode_output

    assert _decode_output(None) == ""
    assert _decode_output(b"hello\n") == "hello"
    assert _decode_output(" raw ") == "raw"


# ── factory ──
def test_factory_returns_host_by_default(monkeypatch):
    monkeypatch.setattr(settings, "bash_sandbox", "host")
    assert isinstance(get_bash_sandbox(), HostBashSandbox)


def test_factory_docker_wires_settings(monkeypatch):
    monkeypatch.setattr(settings, "bash_sandbox", "docker")
    monkeypatch.setattr(settings, "bash_sandbox_image", "debian:bookworm-slim")
    monkeypatch.setattr(settings, "bash_sandbox_cpus", 0.5)
    sb = get_bash_sandbox()
    assert isinstance(sb, DockerBashSandbox)
    assert sb.image == "debian:bookworm-slim"
    assert sb.cpus == 0.5
