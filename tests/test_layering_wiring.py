"""Layering + wiring guards for the final architecture (P4-1 close-out).

Three claims must stay mechanically true, or the build fails:

1. **Reverse-dependency firewall** — ``packages/workflow`` never imports adapter or
   application layers, and the agent stack never imports the workflow core or the
   research plugin (Workflow Core -> nothing domain-shaped; Agent/Skill/Tool -> never
   Workflow).
2. **The definition actually runs** — production call sites exist: the adapter
   resolves through the runtime factory, begin_run stamps the fingerprint, the
   driver facade contains no orchestration (it is a re-export + one construction
   hook only).
3. **The facade froze, not grew** — the compat surface is pure re-exports.
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CORE_DIR = REPO_ROOT / "packages" / "workflow"
AGENT_DIR = REPO_ROOT / "packages" / "agent"
ADAPTER = REPO_ROOT / "plugins" / "research" / "workflow_adapter.py"
SPEC = REPO_ROOT / "plugins" / "research" / "workflow_spec.py"
PLUGIN = REPO_ROOT / "plugins" / "research" / "plugin.py"
FACADE = REPO_ROOT / "plugins" / "research" / "driver.py"

FORBIDDEN_ROOTS = {"plugins", "apps", "core", "agent", "rag", "shared"}


def _import_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and not node.level:
            roots.add((node.module or "").split(".")[0])
    return roots


class TestReverseDependencyFirewall:
    def test_workflow_core_never_imports_domain_layers(self):
        for path in sorted(CORE_DIR.rglob("*.py")):
            bad = _import_roots(path) & FORBIDDEN_ROOTS
            assert not bad, f"{path.name} imports {sorted(bad)}"

    def test_agent_stack_never_imports_workflow_or_research(self):
        for path in sorted(AGENT_DIR.rglob("*.py")):
            roots = _import_roots(path)
            assert "workflow" not in roots, f"{path} imports the workflow core"
            assert "plugins" not in roots, f"{path} imports plugin code"


class TestDefinitionRunsInProduction:
    def _src(self, path: Path) -> str:
        return path.read_text(encoding="utf-8")

    def test_adapter_resolves_through_the_generic_runtime(self):
        src = self._src(ADAPTER)
        assert "from workflow.runtime import MappingRegistry, build_deps" in src
        assert "build_deps(" in src and "MappingRegistry(" in src
        # the logical id comes from the definition layer, never a literal branch
        assert "RESEARCH_EXECUTOR_ID: " in src
        assert '== "research-agent-kernel"' not in src

    def test_adapter_drives_one_iteration_through_the_core(self):
        src = self._src(ADAPTER)
        assert "drive_iteration(" in src and "RESEARCH_WORKFLOW" in src

    def test_spec_is_imported_by_the_production_layers(self):
        assert "workflow_spec" in self._src(ADAPTER)
        assert "RESEARCH_WORKFLOW.fingerprint()" in self._src(PLUGIN)  # begin_run stamp
        assert "RESEARCH_WORKFLOW.fingerprint()" in self._src(ADAPTER)  # per-turn check

    def test_facade_holds_no_orchestration(self):
        src = self._src(FACADE)
        tree = ast.parse(src)
        # Look at CODE symbols only (docstrings may describe the architecture).
        code_names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                code_names.update(a.name for a in node.names)
                code_names.add(node.module or "")
            elif isinstance(node, ast.Call):
                f = node.func
                code_names.add(f.id if isinstance(f, ast.Name) else getattr(f, "attr", ""))
            elif isinstance(node, ast.Attribute):
                code_names.add(node.attr)
        banned = {"drive_iteration", "RunnerDeps", "LeaseLedger", "atomic_update",
                  "LoopPolicy", "RetryPolicy"}
        assert not code_names & banned, f"facade grew orchestration: {code_names & banned}"
        funcs = [n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
        assert funcs == ["__init__"], f"facade defines new logic: {funcs}"
        # and the fingerprint algorithm itself lives in the core, used as a helper
        assert "compute_fingerprint" in self._src(SPEC)
