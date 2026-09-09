"""Cross-VM file-lock integration test (REAL topology, not a mock).

Cells (production shape: portalocker ``LOCK_EX|LOCK_NB`` retried to a timeout, exactly
``plugins/research/plugin.py``'s ``atomic_update_project`` call):

  A  host holder     -> host probe         control: same-VM exclusion must WORK
  B  container holder -> container probe   control: same-VM exclusion must WORK
  C  host holder     -> container probe    cross-VM direction 1 (API holds, worker probes)
  D  container holder -> host probe        cross-VM direction 2 (worker holds, API probes)

The controls are asserted: if a same-VM ``LOCK_EX`` stops being exclusive, the scratch
store's mutual-exclusion contract is broken *within* a VM and every atomic commit is
at risk — that failure must page.

The cross-VM cells are REPORTED, not asserted-pass: a verdict of "invisible" is a
deployment-topology finding (it is the honest outcome recorded on 2026-09-09 over the
Docker Desktop ``./data`` bind mount) and the fix is a topology decision, not a test
weakening. The matrix is printed as machine-readable JSON for the report.

Skips when docker/``deepdive-worker`` or the probe task dir is absent (e.g. CI).
Run directly:  pytest tests/test_cross_vm_lock_integration.py -s -q
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import portalocker
import pytest

ROOT = Path(__file__).resolve().parents[1]
TASK_DIR = (
    ROOT / "data" / "research_scratch"
    / "61f6bb80-ca7c-4412-bed3-ced2853a45c6" / "0251f1ee-4c29-4c68-b64b-0b6666a9c6d7"
)
LK_HOST = TASK_DIR / ".project.lock"
LK_CONT = "/app/data/research_scratch/61f6bb80-ca7c-4412-bed3-ced2853a45c6/0251f1ee-4c29-4c68-b64b-0b6666a9c6d7/.project.lock"
HOLDER_TAG = "XVMLOCKHOLD"
HOLDER_S = 12

_CODE = """
import portalocker, time, json
lk = {lk!r}
if {hold}:
    with portalocker.Lock(lk, flags=portalocker.LOCK_EX, timeout=15) as fh:
        print("HOLD-ACQUIRED", flush=True)
        time.sleep({hold_s})
    print("HOLD-RELEASED", flush=True)
else:
    t0 = time.time()
    try:
        with portalocker.Lock(lk, flags=portalocker.LOCK_EX | portalocker.LOCK_NB,
                              timeout=6, check_interval=0.2):
            print(json.dumps({{"acquired": True, "wait": round(time.time()-t0, 2)}}))
    except portalocker.AlreadyLocked:
        print(json.dumps({{"acquired": False, "err": "AlreadyLocked",
                           "wait": round(time.time()-t0, 2)}}))
    except portalocker.LockException as e:
        print(json.dumps({{"acquired": False, "err": type(e).__name__,
                           "wait": round(time.time()-t0, 2)}}))
"""


def _env() -> dict:
    e = dict(os.environ, PYTHONUNBUFFERED="1")
    e.pop("ELECTRON_RUN_AS_NODE", None)
    e["MSYS_NO_PATHCONV"] = "1"       # git-bash rewrites /app/... argv paths
    e["MSYS2_ARG_CONV_EXCL"] = "*"
    return e


def _docker_worker_up() -> bool:
    if not shutil.which("docker"):
        return False
    r = subprocess.run(
        ["docker", "ps", "--filter", "name=deepdive-worker",
         "--filter", "status=running", "--format", "{{.Names}}"],
        capture_output=True, text=True, env=_env(), timeout=30)
    return "deepdive-worker" in r.stdout


pytestmark = pytest.mark.skipif(
    not (TASK_DIR.is_dir() and _docker_worker_up()),
    reason="requires the task dir on ./data and a running deepdive-worker container",
)


def _host_holder():
    code = _CODE.format(lk=str(LK_HOST), hold=True, hold_s=HOLDER_S)
    return subprocess.Popen(
        [sys.executable, "-c", code], cwd=ROOT, env=_env(),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )


def _wait_host_holder(proc) -> bool:
    while True:
        line = proc.stdout.readline()
        if not line:
            return False
        if "HOLD-ACQUIRED" in line:
            return True


def _host_probe() -> dict:
    code = _CODE.format(lk=str(LK_HOST), hold=False, hold_s=0)
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=_env(),
                       capture_output=True, text=True, timeout=60)
    out = r.stdout.strip()
    assert out, f"host probe produced no output: {r.stderr[-300:]}"
    return json.loads(out.splitlines()[-1])


def _container_holder() -> None:
    code = (
        f"import portalocker, time; _tag='{HOLDER_TAG}'; "
        "exec(\"with portalocker.Lock(%r, flags=portalocker.LOCK_EX, "
        "timeout=15) as fh:\\n print('HOLD-ACQUIRED', flush=True); "
        f"time.sleep({HOLDER_S})\")" % LK_CONT
    )
    subprocess.run(
        ["docker", "exec", "-d", "deepdive-worker", "python", "-c", code],
        check=True, capture_output=True, env=_env(), timeout=30)


def _wait_container_holder() -> bool:
    deadline = time.time() + 10
    probe = (
        f"for p in /proc/[0-9]*/cmdline; do tr '\\0' ' ' < $p 2>/dev/null "
        f"| grep -q {HOLDER_TAG} && tr '\\0' ' ' < $p | grep -q python "
        "&& echo hit && break; done"
    )
    while time.time() < deadline:
        out = subprocess.run(
            ["docker", "exec", "deepdive-worker", "sh", "-c", probe],
            capture_output=True, text=True, env=_env(), timeout=30)
        if "hit" in out.stdout:
            time.sleep(0.5)  # let the holder take the lock before probing
            return True
        time.sleep(0.2)
    return False


def _kill_container_holder() -> None:
    kill = (
        f"for p in /proc/[0-9]*/cmdline; do tr '\\0' ' ' < $p 2>/dev/null "
        f"| grep -q {HOLDER_TAG} && tr '\\0' ' ' < $p | grep -q python && {{ "
        "pid=$(echo $p | sed 's|/proc/||; s|/cmdline||'); kill $pid 2>/dev/null; }; "
        "done; true"
    )
    subprocess.run(
        ["docker", "exec", "deepdive-worker", "sh", "-c", kill],
        capture_output=True, env=_env(), timeout=30)


def _container_probe() -> dict:
    code = _CODE.format(lk=LK_CONT, hold=False, hold_s=0)
    r = subprocess.run(
        ["docker", "exec", "deepdive-worker", "python", "-c", code],
        capture_output=True, text=True, env=_env(), timeout=60)
    out = r.stdout.strip()
    assert out, f"container probe produced no output: {r.stderr[-300:]}"
    return json.loads(out.splitlines()[-1])


def test_cross_vm_lock_matrix():
    results: dict[str, dict] = {}
    try:
        # Cell A — host holder -> host probe (control, must be refused)
        h = _host_holder()
        assert _wait_host_holder(h), "host holder failed to acquire"
        results["A_host_to_host"] = _host_probe()
        h.wait(timeout=HOLDER_S + 15)

        # Cell C — host holder -> container probe (cross-VM direction 1)
        h = _host_holder()
        assert _wait_host_holder(h), "host holder failed to acquire"
        results["C_host_to_container"] = _container_probe()
        h.wait(timeout=HOLDER_S + 15)

        # Cell B — container holder -> container probe (control, must be refused)
        _container_holder()
        assert _wait_container_holder(), "container holder never appeared"
        results["B_container_to_container"] = _container_probe()
    finally:
        _kill_container_holder()
        time.sleep(1)

    # Cell D — container holder -> host probe (cross-VM direction 2)
    _container_holder()
    try:
        assert _wait_container_holder(), "container holder never appeared"
        results["D_container_to_host"] = _host_probe()
    finally:
        _kill_container_holder()

    print("\nXVM-LOCK-MATRIX " + json.dumps(results))
    verdict = {
        "host_vs_host_exclusion": results["A_host_to_host"]["acquired"] is False,
        "container_vs_container_exclusion":
            results["B_container_to_container"]["acquired"] is False,
        "host_lock_visible_to_container":
            results["C_host_to_container"]["acquired"] is False,
        "container_lock_visible_to_host":
            results["D_container_to_host"]["acquired"] is False,
    }
    print("XVM-LOCK-VERDICT " + json.dumps(verdict))

    # Controls: same-VM mutual exclusion is the scratch store's contract.
    assert verdict["host_vs_host_exclusion"], (
        "portalocker LOCK_EX stopped excluding host-vs-host: atomic_update_project's "
        f"in-process contract is broken ({results['A_host_to_host']})")
    assert verdict["container_vs_container_exclusion"], (
        "portalocker LOCK_EX stopped excluding container-vs-container: the worker's "
        f"commit path lost its lease ({results['B_container_to_container']})")
    # Cross-VM cells: reported above; NOT asserted either way — the honest verdict is
    # a topology finding, changing the deployment is the decision this informs.
