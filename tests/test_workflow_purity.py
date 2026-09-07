"""Domain-purity guards for Workflow Core (P4-1 red line 1, three-layer defense).

(a) dependency direction — every ``packages/workflow`` module may import ONLY the standard
    library, typing infrastructure, and sibling ``workflow.*`` modules; any import of the
    application or adapter layers (plugins / apps / core / agent / rag) fails the suite;
(b) vocabulary — a word-boundary scan for business-domain terms over the core sources,
    with an explicit allowlist for generic English that must never be banned outright
    (the scan is an assistive safety net, not the sole judge — type-shape review stays
    with the abstract Protocol checks in ``ports``);
(c) test independence — the core test files themselves may not import adapter fixtures.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CORE_DIR = REPO_ROOT / "packages" / "workflow"
TESTS_DIR = Path(__file__).resolve().parent

FORBIDDEN_ROOT_IMPORTS = {"plugins", "apps", "core", "agent", "rag", "shared"}

# Word-boundary matched; plurals explicit. Chosen so generic control-plane English
# (task, transition, validate, lease, ledger, slot, signal...) passes untouched.
DOMAIN_TERMS = [
    "claim", "claims", "evidence", "citations?", "gate", "gates",
    "stage", "stages", "research", "verify", "verdict", "anchored",
    "scrape", "approvals?", "stalled",
]
_DOMAIN_RE = re.compile(
    r"\b(" + "|".join(DOMAIN_TERMS) + r")\b", re.IGNORECASE
)


def _core_sources() -> list[Path]:
    return sorted(CORE_DIR.glob("*.py"))


class TestDependencyDirection:
    def test_core_sources_exist(self):
        assert _core_sources(), "packages/workflow must contain the core modules"

    @pytest.mark.parametrize("path", _core_sources(), ids=lambda p: p.name)
    def test_no_upward_or_sideward_imports(self, path: Path):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            roots: list[str] = []
            if isinstance(node, ast.Import):
                roots = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:  # relative import: always intra-package
                    continue
                roots = [(node.module or "").split(".")[0]]
            for root in roots:
                assert root not in FORBIDDEN_ROOT_IMPORTS, (
                    f"{path.name} imports {root!r} — Workflow Core may not depend on "
                    f"adapters or application layers"
                )
                if root == "workflow":  # the only allowed intra-core sibling
                    continue
                # Everything else must be stdlib/typing infrastructure: pin the small
                # allowlist the core is actually written against instead of guessing.
                assert root in {
                    "__future__", "typing", "collections", "copy", "dataclasses",
                    "enum", "json", "hashlib", "uuid", "datetime", "asyncio", "time",
                    "contextlib", "abc", "typing_extensions",
                }, f"{path.name}: unexpected import root {root!r}"


class TestVocabulary:
    @pytest.mark.parametrize("path", _core_sources(), ids=lambda p: p.name)
    def test_no_domain_vocabulary(self, path: Path):
        hits = []
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if (m := _DOMAIN_RE.search(line)) is not None:
                hits.append(f"{path.name}:{lineno}: {m.group(0)!r}")
        assert not hits, "domain terms in core sources: " + "; ".join(hits)


class TestSuiteIndependence:
    def test_core_tests_never_import_adapters(self):
        for path in sorted(TESTS_DIR.glob("test_workflow_*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    roots = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and not node.level:
                    roots = [(node.module or "").split(".")[0]]
                else:
                    continue
                for root in roots:
                    assert root not in FORBIDDEN_ROOT_IMPORTS, (
                        f"{path.name} imports adapter layer {root!r}"
                    )
